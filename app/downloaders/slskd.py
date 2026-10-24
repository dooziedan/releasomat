"""Soulseek downloader: finds and downloads a track through the owner's slskd (ADR 0008).

slskd is a separate program the owner runs. Releasomat only talks to its REST API (address and API
key from Settings, the key goes in the ``X-API-Key`` header) and picks the finished files up from
slskd's download folder (SLSKD_DOWNLOADS_DIR, mounted at ``/slskd``).

What happens for one track:

1. **Search** Soulseek for "artist title" as the playlist writes it. If nobody answers, a simpler
   search follows: the song without versions, no accents, and a ``*`` instead of the artist's
   first letter ("*aft punk get lucky"), because the Soulseek server hides some artists from
   exact searches (Explo #141). Only one search runs at a time: slskd answers a second one with
   HTTP 429 (Explo #79).
2. **Pick** files: a wanted format (SLSKD_FORMATS), lossy files with at least SLSKD_MIN_BITRATE,
   a length within 10 seconds, a path that names the title and the artist or album, no other
   version ("live", "remix" …), and a peer with a free upload slot. The best comes first: format
   order, then quality (bit depth or bitrate), then the peer's queue and speed. One file per peer.
3. **Download** it as an slskd "batch" saved in slskd's folder ``releasomat/<download id>/``, and
   watch it. If the peer refuses, the transfer breaks off, stalls or takes too long, the next
   peer is tried (up to ``MAX_TRIES``); slskd itself would only retry the same peer (Explo #122).
4. **Check** the file where slskd put it (readable audio, length, bitrate) and move it into the
   staging folder with ``files.take_slskd_download``. A wrong file stays in slskd's folder:
   Releasomat never deletes music on its own (ADR 0003); the owner decides.

Everything that goes wrong becomes a DownloaderError in plain words, with ``tried``: one entry per
peer and file and why it wasn't used. The API key never appears in a message or log.

When the refresh is stopped (Stop button), slskd is told at once: a running search is stopped and
deleted, a transfer is cancelled and taken off slskd's list. A file slskd had already finished
stays in its folder (ADR 0003).
"""

import asyncio
import logging
import re
import uuid
import weakref
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
from rapidfuzz import fuzz

from app import files, httpclient, matching
from app.downloaders.base import Downloader, DownloaderError, DownloaderNotReady, DownloadResult
from app.sources.base import SourceTrack
from app.tagging import TaggingError, audio_info

log = logging.getLogger(__name__)

NOT_CONNECTED = "Soulseek isn't connected: add its address and API key in Settings."
DOWNLOADER = "slskd"
DEFAULT_FORMATS = ["flac", "aiff", "wav", "mp3"]  # the order is the preference
LOSSLESS = {"flac", "aiff", "wav", "alac", "ape", "wv"}  # every other format counts as lossy
ALIASES = {"aif": "aiff", "wave": "wav"}
# Audio formats at all; anything else (cover.jpg, .nfo, .cue) is never a candidate.
AUDIO = LOSSLESS | {"mp3", "m4a", "aac", "ogg", "opus", "wma"}
MIN_VERSION = (0, 26)  # download batches with a destination folder came with slskd 0.26

MAX_TRIES = 3  # peers tried per track
POLL = 1.0  # seconds between two looks at a search or a transfer
SEARCH_TIME = 45.0  # seconds a search may run before Releasomat stops it and takes what's there
STALL_LIMIT = 120.0  # seconds a transfer may go without any progress
MAX_TIME = 15 * 60.0  # seconds one transfer may take in all
FILE_WAIT = 20.0  # seconds to wait for slskd to move a finished file into its folder
BUSY_RETRIES = 5  # how often a call answered with 429 ("busy") is repeated
BUSY_PAUSE = 2.0  # seconds before repeating it
MAX_REJECTED_SHOWN = 6  # rejected search results listed one by one in the error report


# --- Search texts -------------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    """Lowercase words with their accents: "Beyoncé – Halo!" -> ["beyoncé", "halo"]."""
    return "".join(c if c.isalnum() else " " for c in text.casefold()).split()


def _wildcard(words: list[str]) -> list[str]:
    """The artist with a ``*`` for its first letter (after a leading "the"): Explo #141."""
    words = list(words)
    i = 1 if len(words) > 1 and words[0] == "the" else 0
    if words and len(words[i]) > 1:
        words[i] = "*" + words[i][1:]
    return words


def search_queries(track: SourceTrack) -> list[str]:
    """The search texts, in order: the exact one ("beyoncé halo", accents as the playlist writes
    them) and a simpler one ("*eyonce halo") for when the first finds nothing."""
    plain = [w for w in matching.search_text(track.artist, track.title).split() if len(w) > 1]
    spelled: dict[str, str] = {}
    for word in _words(f"{track.artist} {track.title}"):
        spelled.setdefault(matching.strip_accents(word), word)
    exact = " ".join(spelled.get(w, w) for w in plain)
    song = [w for w in matching.title_parts(track.title).song.split() if len(w) > 1]
    simple = " ".join([*_wildcard(matching.main_artist(track.artist).split()), *song])
    return list(dict.fromkeys(q for q in (exact, simple) if q))


# --- Choosing files ------------------------------------------------------------------------------


def clock(seconds: float | None) -> str:
    """225 -> "3:45"."""
    if not seconds:
        return "?"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d}"


def normalise_formats(formats: list[str] | None) -> list[str]:
    """["FLAC", ".aif", "mp3"] -> ["flac", "aiff", "mp3"]."""
    cleaned = (f.strip().lower().lstrip(".") for f in (formats or DEFAULT_FORMATS))
    return list(dict.fromkeys(ALIASES.get(f, f) for f in cleaned if f))


@dataclass
class Candidate:
    """One file one peer offers, from slskd's search responses."""

    username: str
    filename: str  # the peer's path, with backslashes: "@@abcd\Music\Artist\Album\01 Song.flac"
    size: int
    length: int | None  # seconds
    bit_rate: int | None  # kbit/s
    bit_depth: int | None
    free_slot: bool
    queue_length: int
    upload_speed: int  # bytes per second

    @property
    def parts(self) -> list[str]:
        return [p for p in self.filename.replace("\\", "/").split("/") if p]

    @property
    def name(self) -> str:
        return self.parts[-1] if self.parts else self.filename

    @property
    def folder(self) -> str:
        return self.parts[-2] if len(self.parts) > 1 else ""

    @property
    def ext(self) -> str:
        """The format from the file name: slskd's own "extension" field is unreliable
        (Explo #202)."""
        suffix = self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""
        return ALIASES.get(suffix, suffix)

    @property
    def lossless(self) -> bool:
        return self.ext in LOSSLESS

    @property
    def quality(self) -> str:
        """ "FLAC 16 bit", "MP3 320 kbit/s"."""
        kind = self.ext.upper() or "?"
        if self.lossless:
            return f"{kind} {self.bit_depth} bit" if self.bit_depth else kind
        return f"{kind} {self.bit_rate} kbit/s" if self.bit_rate else f"{kind}, bitrate unknown"

    @property
    def label(self) -> str:
        return f"Soulseek: {self.username} · {self.name} ({self.quality}, {clock(self.length)})"


def candidates_from(responses: list[dict]) -> list[Candidate]:
    """Every downloadable file in slskd's search responses (locked files are left out)."""
    found = []
    for response in responses or []:
        for file in response.get("files") or []:
            if not file.get("filename"):
                continue
            found.append(
                Candidate(
                    username=response.get("username") or "?",
                    filename=file["filename"],
                    size=int(file.get("size") or 0),
                    length=file.get("length"),
                    bit_rate=file.get("bitRate"),
                    bit_depth=file.get("bitDepth"),
                    free_slot=bool(response.get("hasFreeUploadSlot")),
                    queue_length=int(response.get("queueLength") or 0),
                    upload_speed=int(response.get("uploadSpeed") or 0),
                )
            )
    return found


def _plain(text: str) -> str:
    """Lowercase words without accents, between spaces, for "contains" checks:
    "Beyoncé\\I Am... Sasha Fierce" -> " beyonce i am sasha fierce "."""
    text = matching.strip_accents(text).casefold().replace("&", " and ")
    return " " + " ".join("".join(c if c.isalnum() else " " for c in text).split()) + " "


def _contains(haystack: str, needle: str, threshold: int) -> bool:
    """True when the words ``needle`` appear in ``haystack`` (both from ``_plain``); longer
    names may be spelled a little differently ("dont" / "don t")."""
    needle = needle.strip()
    if not needle:
        return False
    if f" {needle} " in haystack:
        return True
    return len(needle) >= 6 and fuzz.partial_ratio(needle, haystack) >= threshold


def _album_name(album: str | None) -> str:
    """The album without bracketed notes: "Random Access Memories (Deluxe)" ->
    "random access memories"."""
    return _plain(re.split(r"\s*[(\[]", album or "")[0]).strip()


# A track number at the start of a file name: "01 - ", "09. ", "[09] ", "07 ".
_TRACK_NUMBER = re.compile(r"^\s*[\[(]?\d{1,3}[\])]?(?:\s*[-.]\s*|\s+)(?=\S)")
# Web addresses some uploaders add to file names: "[www.slider.kz]".
_WEB_NOTE = re.compile(
    r"\s*[\[(][^\])]*(?:www\.|https?:|\.(?:com|net|org|kz|ru))[^\])]*[\])]", re.IGNORECASE
)
_DASHES = re.compile(r"\s+[-–—]\s+")


def file_title(stem: str, artist: str) -> str:
    """The title part of a file name, without track number, artist and web notes:
    "09 - Sota - Harter Street VIP [www.slider.kz]" -> "Harter Street VIP". Soulseek files are
    mostly named "01 - Title" or "Artist - Title"; read as a whole, the part after the dash would
    look like a version ("- Harter Street VIP")."""
    text = _WEB_NOTE.sub("", stem.replace("_", " "))
    text = _TRACK_NUMBER.sub("", text)
    parts = _DASHES.split(text)
    # "Artist - Album - 01 - Title": the title comes after the last part that is only a number.
    numbers = [i for i, part in enumerate(parts[:-1]) if _plain(part).strip().isdigit()]
    if numbers:
        parts = parts[numbers[-1] + 1 :]
    names = {_plain(a).strip() for a in [artist, *matching.split_artists(artist)]} - {""}
    while len(parts) > 1:
        first = _plain(parts[0])
        if first.strip().isdigit() or any(f" {name} " in first for name in names):
            parts = parts[1:]
        else:
            break
    return " - ".join(parts)


# Notes like "original mix" or "radio edit" that don't change the track, also without brackets
# ("Rocker Original Mix"); longest first.
_SOFT_PHRASES = sorted((v for v in matching.SOFT_VERSIONS if " " in v), key=len, reverse=True)


def _version_words(parts: matching.TitleParts) -> set[str]:
    """The words that make a different version, from the song and its versions together, so
    "Harter Street VIP" and "Harter Street (VIP)" count as the same version."""
    text = f" {parts.song} {' '.join(parts.versions)} "
    for phrase in _SOFT_PHRASES:
        text = text.replace(f" {phrase} ", " ")
    return set(text.split()) & matching.HARD_VERSION_WORDS


# Why a file was rejected, by kind, as the summary in the error report says it. The first kinds
# are the right track in the wrong quality: the error report lists those first.
KINDS = {
    "slot": "peers without a free upload slot",
    "bitrate": "too low bitrate",
    "format": "other formats",
    "length": "other lengths",
    "version": "other versions",
    "artist": "other artists",
    "title": "other songs",
}


def check_candidate(
    track: SourceTrack, c: Candidate, formats: list[str], min_bitrate: int, threshold: int
) -> tuple[str, str] | None:
    """Why a file is not wanted, as (kind, reason); None if it may be downloaded. Whether it is
    the track comes first, then the quality rules."""
    if c.ext not in AUDIO:
        return "format", f"not an audio file ({c.ext.upper() or 'no extension'})"
    path = _plain(c.filename)
    if not _contains(path, matching.title_parts(track.title).song, threshold):
        return "title", "the path doesn't name the title"
    album = _album_name(track.album)
    artist_named = _contains(path, matching.main_artist(track.artist), threshold)
    if not artist_named and not (album and _contains(path, album, threshold)):
        return "artist", "the path names neither the artist nor the album"
    stem = c.name.rsplit(".", 1)[0]
    unwanted = matching.unwanted_words(track.artist, track.title, f"{c.folder} {stem}")
    if unwanted:
        return "version", f"another version ({', '.join(sorted(unwanted))})"
    wanted_parts = matching.title_parts(track.title)
    named = _plain(f"{c.folder} {stem}")
    missing = [v for v in wanted_parts.versions if f" {v} " not in named]
    if missing:
        return "version", f"not the {', '.join(missing)}"
    found_parts = matching.title_parts(file_title(stem, track.artist))
    extra = _version_words(found_parts) - _version_words(wanted_parts)
    if extra:
        named_versions = ", ".join(found_parts.versions) or ", ".join(sorted(extra))
        return "version", f"another version ({named_versions})"
    if not matching.durations_agree(track.duration, c.length):
        return "length", f"{clock(c.length)} long instead of {clock(track.duration)}"
    if c.ext not in formats:
        wanted_formats = ", ".join(f.upper() for f in formats)
        return "format", f"{c.ext.upper() or 'unknown format'} isn't wanted ({wanted_formats})"
    if not c.lossless and c.bit_rate and c.bit_rate < min_bitrate:
        return "bitrate", f"{c.bit_rate} kbit/s, at least {min_bitrate} wanted"
    if not c.free_slot:
        return "slot", f"the peer has no free upload slot (queue: {c.queue_length})"
    return None


def rank(c: Candidate, formats: list[str]) -> tuple:
    """Sort key, best first: format order, then bit depth (lossless) or bitrate (lossy; unknown
    last), then a short queue and a fast peer."""
    order = formats.index(c.ext) if c.ext in formats else len(formats)
    quality = (c.bit_depth or 16) if c.lossless else (c.bit_rate or 0)
    return (order, -quality, c.queue_length, -c.upload_speed)


@dataclass
class Choice:
    """The outcome of looking at a search: files to try (best first, one per peer) and the
    rejected ones with their reasons."""

    usable: list[Candidate]
    rejected: list[tuple[Candidate, str, str]]  # (file, kind, reason)

    def tried(self) -> list[dict]:
        """The rejected files for the error report: the best-looking few one by one, the rest
        counted by reason."""
        shown = self.rejected[:MAX_REJECTED_SHOWN]
        entries = [_entry(c, reason) for c, _, reason in shown]
        rest = self.rejected[MAX_REJECTED_SHOWN:]
        if rest:
            counts = Counter(kind for _, kind, _ in rest)
            summary = ", ".join(f"{n} × {KINDS[kind]}" for kind, n in counts.most_common())
            entries.append(
                {
                    "downloader": DOWNLOADER,
                    "candidate": f"Soulseek: {len(rest)} more file{'s' if len(rest) > 1 else ''}",
                    "reason": summary,
                }
            )
        return entries

    def summary(self) -> str:
        counts = Counter(kind for _, kind, _ in self.rejected)
        return ", ".join(f"{n} × {KINDS[kind]}" for kind, n in counts.most_common())


def choose(
    track: SourceTrack,
    responses: list[dict],
    formats: list[str],
    min_bitrate: int,
    threshold: int,
) -> Choice:
    """Sort the files of a search into usable (best first, one per peer) and rejected."""
    usable, rejected = [], []
    for c in candidates_from(responses):
        problem = check_candidate(track, c, formats, min_bitrate, threshold)
        if problem:
            rejected.append((c, *problem))
        else:
            usable.append(c)
    usable.sort(key=lambda c: rank(c, formats))
    best_per_peer = list({c.username: c for c in reversed(usable)}.values())
    best_per_peer.sort(key=lambda c: rank(c, formats))
    kinds = list(KINDS)
    rejected.sort(key=lambda r: (kinds.index(r[1]), rank(r[0], formats)))
    return Choice(best_per_peer, rejected)


def _entry(c: Candidate, reason: str) -> dict:
    return {"downloader": DOWNLOADER, "candidate": c.label, "reason": reason}


# --- Talking to slskd ---------------------------------------------------------------------------


def clean_url(url: str | None) -> str:
    """ "192.168.1.5:5030/" -> "http://192.168.1.5:5030"; a pasted "/api/v0" is dropped."""
    text = (url or "").strip().rstrip("/")
    text = re.sub(r"/api/v0$", "", text, flags=re.IGNORECASE)
    if text and "://" not in text:
        text = f"http://{text}"
    return text


def _excerpt(response: httpx.Response) -> str:
    return " ".join(response.text.split())[:300]


class _Api:
    """slskd's REST API (``<address>/api/v0``). Connection problems, a wrong key and "busy"
    answers become DownloaderErrors in plain words; other answers go back to the caller."""

    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client, self.url = client, url

    async def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(BUSY_RETRIES + 1):
            try:
                response = await self.client.request(method, path, **kwargs)
            except httpx.TimeoutException:
                raise DownloaderError(
                    f"slskd at {self.url} didn't answer in time.", step=path
                ) from None
            except httpx.HTTPError as exc:
                reason = str(exc).strip()[:200] or type(exc).__name__
                raise DownloaderError(
                    f"Releasomat can't reach slskd at {self.url} ({reason}). Check the address "
                    "and that slskd is running.",
                    step=path,
                ) from None
            if response.status_code == 401:
                raise DownloaderError(
                    "slskd refused the API key (HTTP 401): check it in Settings.", http_status=401
                )
            if response.status_code == 403:
                raise DownloaderError(
                    "slskd refused access (HTTP 403): the API key may not be allowed from "
                    "Releasomat's address.",
                    http_status=403,
                )
            if response.status_code != 429 or attempt == BUSY_RETRIES:
                return response
            await asyncio.sleep(BUSY_PAUSE)  # slskd is busy with another request: wait a bit
        raise AssertionError("unreachable")

    async def quietly(self, method: str, path: str) -> None:
        """Tidying up (delete a search, remove a transfer from slskd's list): a failure only
        gets logged."""
        try:
            await self.client.request(method, path)
        except httpx.HTTPError as exc:
            log.info("slskd: %s %s failed: %s", method, path, type(exc).__name__)

    def unexpected(self, response: httpx.Response, doing: str) -> DownloaderError:
        if response.status_code == 429:
            message = f"slskd stayed busy while {doing} (HTTP 429). Try again later."
        else:
            message = f"slskd answered HTTP {response.status_code} while {doing}."
        return DownloaderError(
            message, http_status=response.status_code, excerpt=_excerpt(response)
        )


def _client(url: str, api_key: str, transport: httpx.AsyncBaseTransport | None):
    return httpclient.client(
        base_url=f"{url}/api/v0", headers={"X-API-Key": api_key}, transport=transport
    )


# One lock per event loop: only one Soulseek search at a time, across every download running
# (slskd answers a second search request with 429, Explo #79).
_search_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _search_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _search_locks.get(loop)
    if lock is None:
        lock = _search_locks[loop] = asyncio.Lock()
    return lock


FAILED_STATES = {
    "Rejected": "the peer refused the download",
    "TimedOut": "the peer didn't answer in time",
    "Errored": "the transfer broke off",
    "Cancelled": "the transfer was cancelled in slskd",
    "Aborted": "the transfer was aborted",
}


def _failure(state: str, exception: str | None) -> str:
    """ "Completed, Rejected" + "File not shared." -> "the peer refused the download (File not
    shared.)"."""
    words = next((text for key, text in FAILED_STATES.items() if key in state), None)
    words = words or f"the transfer ended as “{state}”"
    return f"{words} ({exception.strip()[:200]})" if exception else words


def _seconds(value: float) -> str:
    return f"{round(value / 60)} minutes" if value >= 120 else f"{round(value)} seconds"


class SlskdDownloader(Downloader):
    name = "slskd"
    label = "Soulseek (slskd)"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        downloads_dir: Path | None = None,
        formats: list[str] | None = None,
        min_bitrate: int = 320,
        threshold: int = 85,
        transport: httpx.AsyncBaseTransport | None = None,
        poll: float = POLL,
        search_time: float = SEARCH_TIME,
        stall_limit: float = STALL_LIMIT,
        max_time: float = MAX_TIME,
        file_wait: float = FILE_WAIT,
        max_tries: int = MAX_TRIES,
    ) -> None:
        self.url = clean_url(url)
        self.api_key = (api_key or "").strip()
        self.downloads_dir = Path(downloads_dir) if downloads_dir else None
        self.formats = normalise_formats(formats)
        self.min_bitrate = min_bitrate
        self.threshold = threshold
        self.transport = transport  # tests talk to the fake slskd in-process
        self.poll, self.search_time, self.file_wait = poll, search_time, file_wait
        self.stall_limit, self.max_time, self.max_tries = stall_limit, max_time, max_tries

    def __repr__(self) -> str:  # never shows the API key
        return f"SlskdDownloader(url={self.url!r}, formats={self.formats})"

    @property
    def configured(self) -> bool:
        return bool(self.url and self.api_key)

    def _downloads_dir(self) -> Path:
        if self.downloads_dir is None:
            from app.config import get_config

            self.downloads_dir = get_config().slskd_downloads_dir
        return self.downloads_dir

    async def download(self, track: SourceTrack, staging_dir: Path) -> DownloadResult:
        if not self.configured:
            raise DownloaderNotReady(NOT_CONNECTED)
        downloads_dir = self._downloads_dir()
        if not downloads_dir.is_dir():
            raise DownloaderError(
                f"slskd's download folder isn't there ({downloads_dir}): mount it into "
                "Releasomat's container (Unraid: /slskd), so finished downloads can be picked up."
            )
        async with _client(self.url, self.api_key, self.transport) as client:
            api = _Api(client, self.url)
            queries, responses = [], []
            for query in search_queries(track):
                queries.append(query)
                responses = await self._search(api, query)
                if candidates_from(responses):
                    break
            choice = choose(track, responses, self.formats, self.min_bitrate, self.threshold)
            if not choice.usable:
                raise self._nothing_usable(choice, queries)
            attempts: list[dict] = []
            for candidate in choice.usable[: self.max_tries]:
                try:
                    outcome = await self._fetch(api, candidate, track, downloads_dir, staging_dir)
                except DownloaderError as exc:
                    exc.tried = attempts + [_entry(candidate, str(exc))] + choice.tried()
                    raise
                if isinstance(outcome, DownloadResult):
                    outcome.tried = attempts + choice.tried()
                    return outcome
                log.info("Soulseek: %s didn't work: %s", candidate.label, outcome)
                attempts.append(_entry(candidate, outcome))
        count = len(attempts)
        raise DownloaderError(
            f"{count} Soulseek download{'s' if count != 1 else ''} tried, none worked: "
            f"{attempts[-1]['reason']}",
            tried=attempts + choice.tried(),
            queries=queries,
        )

    def _nothing_usable(self, choice: Choice, queries: list[str]) -> DownloaderError:
        searched = " and ".join(f"“{q}”" for q in queries)
        if not choice.rejected:
            return DownloaderError(
                f"Nobody on Soulseek shares this track (searched for {searched}).",
                queries=queries,
            )
        count = len(choice.rejected)
        return DownloaderError(
            f"Soulseek has {count} file{'s' if count != 1 else ''} for {searched}, but none "
            f"fits: {choice.summary()}.",
            tried=choice.tried(),
            queries=queries,
        )

    # --- searching ---

    async def _search(self, api: _Api, text: str) -> list[dict]:
        """Run one search and return slskd's responses (one per peer)."""
        search_id = str(uuid.uuid4())
        async with _search_lock():
            response = await api.call(
                "POST", "/searches", json={"id": search_id, "searchText": text}
            )
            if response.status_code == 409:
                raise DownloaderError(
                    "slskd isn't logged in to Soulseek right now, so it can't search.",
                    http_status=409,
                    excerpt=_excerpt(response),
                )
            if response.status_code not in (200, 201):
                raise api.unexpected(response, "starting a search")
            try:
                await self._wait_for_search(api, search_id)
                response = await api.call("GET", f"/searches/{search_id}/responses")
                if response.status_code != 200:
                    raise api.unexpected(response, "reading the search results")
                responses = response.json()
            except asyncio.CancelledError:  # stopped: end the search in slskd, too
                await api.quietly("PUT", f"/searches/{search_id}")
                raise
            finally:
                await api.quietly("DELETE", f"/searches/{search_id}")
        return responses if isinstance(responses, list) else []

    async def _wait_for_search(self, api: _Api, search_id: str) -> None:
        loop = asyncio.get_running_loop()
        started, stopped = loop.time(), False
        while True:
            response = await api.call("GET", f"/searches/{search_id}")
            if response.status_code != 200:
                raise api.unexpected(response, "waiting for the search")
            if response.json().get("isComplete"):
                return
            waited = loop.time() - started
            if waited >= self.search_time and not stopped:
                # Enough answers have usually arrived by now: stop it and take what's there.
                await api.call("PUT", f"/searches/{search_id}")
                stopped = True
            elif waited >= self.search_time + 15:
                return  # slskd didn't finish it either; read whatever it has
            await asyncio.sleep(self.poll)

    # --- downloading ---

    async def _fetch(
        self,
        api: _Api,
        c: Candidate,
        track: SourceTrack,
        downloads_dir: Path,
        staging_dir: Path,
    ) -> DownloadResult | str:
        """Download one file and check it: a DownloadResult, or why it didn't work. Raises
        DownloaderError for problems the next peer wouldn't solve (setup, folders)."""
        batch_id = str(uuid.uuid4())
        destination = f"{files.SLSKD_FOLDER}/{batch_id}"
        body = {
            "id": batch_id,
            "username": c.username,
            "files": [{"filename": c.filename, "size": c.size}],
            "options": {"destination": destination, "externalId": batch_id},
        }
        log.info("Soulseek: downloading %s", c.label)
        transfer_id = None
        try:
            response = await api.call("POST", "/transfers/downloads/batches", json=body)
            if response.status_code == 404:
                return f"the peer is offline ({_excerpt(response) or 'HTTP 404'})"
            if response.status_code == 200:
                failures = (response.json() or {}).get("failures") or [{}]
                reason = failures[0].get("message") or "no reason"
                return f"slskd couldn't queue the file ({reason})"
            if response.status_code not in (201, 207):
                excerpt = _excerpt(response)
                return f"slskd didn't queue the download (HTTP {response.status_code}: {excerpt})"
            transfers = ((response.json() or {}).get("batch") or {}).get("transfers") or []
            transfer_id = transfers[0].get("id") if transfers else None
            outcome = await self._watch(api, batch_id, c, transfer_id)
            if isinstance(outcome, str):
                await self._remove(api, c, transfer_id)  # cancels it, too
                return outcome
            path = await self._finished_file(downloads_dir / destination, c)
        except asyncio.CancelledError:  # stopped: cancel the transfer in slskd
            log.info("Soulseek: stopped, cancelling %s", c.label)
            await self._cancel(api, batch_id, c, transfer_id)
            raise
        await self._remove(api, c, transfer_id)
        if path is None:
            raise DownloaderError(
                f"slskd finished the download, but Releasomat doesn't find it in "
                f"{downloads_dir / destination}. Is slskd's download folder mounted there?",
                step="pick up",
            )
        return self._take(path, c, track, downloads_dir, staging_dir, destination)

    async def _remove(self, api: _Api, c: Candidate, transfer_id: str | None) -> None:
        """Cancel an unfinished transfer and take it off slskd's list of downloads."""
        if transfer_id:
            user = quote(c.username, safe="")
            await api.quietly("DELETE", f"/transfers/downloads/{user}/{transfer_id}?remove=true")

    async def _cancel(
        self, api: _Api, batch_id: str, c: Candidate, transfer_id: str | None
    ) -> None:
        """The refresh was stopped: cancel the transfer. If the stop came while slskd was still
        queueing it, its ID isn't known yet: it is looked up in the batch."""
        if transfer_id is None:
            with suppress(DownloaderError):
                transfer = await self._transfer(api, batch_id, c, None)
                transfer_id = transfer.get("id") if transfer else None
        await self._remove(api, c, transfer_id)

    async def _watch(
        self, api: _Api, batch_id: str, c: Candidate, transfer_id: str | None
    ) -> dict | str:
        """Follow the transfer until it ends: its last state if it succeeded, else the reason."""
        loop = asyncio.get_running_loop()
        started = last_change = loop.time()
        last, misses = None, 0
        while True:
            transfer = await self._transfer(api, batch_id, c, transfer_id)
            now = loop.time()
            if transfer is None:
                misses += 1
                if misses >= 3:
                    return "slskd lost track of the transfer"
            else:
                state = transfer.get("state") or ""
                if "Succeeded" in state:
                    return transfer
                if state.startswith("Completed"):
                    return _failure(state, transfer.get("exception"))
                progress = (state, transfer.get("bytesTransferred"))
                if progress != last:
                    last, last_change = progress, now
                if now - last_change >= self.stall_limit:
                    percent = transfer.get("percentComplete") or 0
                    return (
                        f"the transfer stalled: no progress for {_seconds(self.stall_limit)} "
                        f"({state}, {percent:.0f} %)"
                    )
            if now - started >= self.max_time:
                return f"the transfer took longer than {_seconds(self.max_time)}"
            await asyncio.sleep(self.poll)

    async def _transfer(
        self, api: _Api, batch_id: str, c: Candidate, transfer_id: str | None
    ) -> dict | None:
        """The transfer as slskd sees it now (from its batch, or by its own ID)."""
        response = await api.call("GET", f"/transfers/downloads/batches/{batch_id}")
        if response.status_code == 200:
            transfers = (response.json() or {}).get("transfers") or []
            for transfer in transfers:
                if transfer.get("filename") == c.filename:
                    return transfer
        if transfer_id:
            user = quote(c.username, safe="")
            response = await api.call("GET", f"/transfers/downloads/{user}/{transfer_id}")
            if response.status_code == 200:
                return response.json()
        return None

    async def _finished_file(self, folder: Path, c: Candidate) -> Path | None:
        """The finished file in the download's own folder. slskd marks the transfer as done
        just before it moves the file there, so this waits until the file is there and its size
        stays the same."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.file_wait
        expected = folder / c.name.replace("/", "_")
        size = None
        while True:
            plain = expected.is_file() and not expected.is_symlink()
            path = expected if plain else _only_file(folder)
            if path is not None:
                now_size = path.stat().st_size
                if now_size and now_size == size:
                    return path
                size = now_size
            if loop.time() >= deadline:
                return path
            await asyncio.sleep(min(self.poll, 0.5))

    def _take(
        self,
        path: Path,
        c: Candidate,
        track: SourceTrack,
        downloads_dir: Path,
        staging_dir: Path,
        destination: str,
    ) -> DownloadResult | str:
        """Check the file where slskd put it, then move it into the staging folder."""
        stays = f"it stays in slskd's folder: {destination}/{path.name}"
        try:
            info = audio_info(path)
        except TaggingError as exc:
            return f"the file isn't readable audio ({exc}); {stays}"
        expected = track.duration or c.length
        if not matching.durations_agree(expected, info.duration):
            return f"the file is {clock(info.duration)} long instead of {clock(expected)}; {stays}"
        if not c.lossless and info.bitrate and info.bitrate < self.min_bitrate * 0.9:
            return f"the file has only {info.bitrate} kbit/s; {stays}"
        try:
            staged = files.take_slskd_download(downloads_dir, path, staging_dir)
        except (files.UnsafePath, OSError) as exc:
            raise DownloaderError(
                f"Releasomat can't move the download out of slskd's folder: {exc}",
                step="pick up",
            ) from None
        return DownloadResult(
            path=staged,
            format=info.format,
            bitrate=info.bitrate,
            downloader=self.name,
            origin=f"Soulseek: {c.username} · {c.name}",
            duration=info.duration,
        )


def _only_file(folder: Path) -> Path | None:
    """The one plain file in a download's folder (slskd may have renamed it, "name_<n>.flac")."""
    if not folder.is_dir() or folder.is_symlink():
        return None
    found = [p for p in folder.iterdir() if p.is_file() and not p.is_symlink()]
    return found[0] if len(found) == 1 else None


# --- The Test button in Settings ----------------------------------------------------------------


def _version(text: str) -> tuple[int, int] | None:
    match = re.match(r"\s*(\d+)\.(\d+)", text or "")
    return (int(match[1]), int(match[2])) if match else None


async def check_connection(
    url: str, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    """Plain message for the Settings page's Test button, e.g. "Connected to slskd 0.26.1,
    logged in to Soulseek." Raises DownloaderError with a plain message (wrong key, unreachable,
    not logged in to Soulseek, too old)."""
    address, key = clean_url(url), (api_key or "").strip()
    if not address or not key:
        raise DownloaderError("Add slskd's address and API key first.")
    async with _client(address, key, transport) as client:
        response = await _Api(client, address).call("GET", "/application")
    if response.status_code != 200:
        raise DownloaderError(
            f"{address} answers, but not like slskd (HTTP {response.status_code}). Check the "
            "address and port (slskd's web port, usually 5030).",
            http_status=response.status_code,
        )
    try:
        state = response.json()
        version = str((state.get("version") or {}).get("current") or "")
        server = state.get("server") or {}
    except (ValueError, AttributeError):
        raise DownloaderError(
            f"{address} answers, but not like slskd. Check the address and port."
        ) from None
    shown = version or "(unknown version)"
    number = _version(version)
    if number is not None and number < MIN_VERSION:
        raise DownloaderError(
            f"slskd {shown} is too old: Releasomat needs slskd 0.26 or newer. Please update it."
        )
    logged_in = server.get("isLoggedIn")
    if logged_in is None:
        logged_in = "LoggedIn" in str(server.get("state") or "")
    if not logged_in:
        raise DownloaderError(
            f"slskd {shown} answers, but isn't logged in to Soulseek "
            f"({server.get('state') or 'not connected'}). Check its Soulseek user name and "
            "password, or wait until it has reconnected."
        )
    return f"Connected to slskd {shown}, logged in to Soulseek."
