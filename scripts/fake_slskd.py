"""A fake slskd for developing and testing Releasomat's Soulseek support (ADR 0008).

It answers like slskd 0.26.1's REST API (same paths, JSON field names, states and ``X-API-Key``
check) but never connects to the Soulseek network: no account, no slskd image, no other people's
files. Its "peers" share the songs of the demo library (``dev/library/Demo``, tags read with
mutagen) plus a few made-up songs. A download "transfers" over a few seconds, with slskd's
states, and then writes a real audio file (a quiet tone made with ffmpeg, as long as the song)
into the downloads folder, laid out like slskd does it.

    .venv/bin/python scripts/fake_slskd.py                    # localhost:5030, into dev/slskd
    .venv/bin/python scripts/fake_slskd.py --port 5039 --downloads-dir /tmp/slskd --speed 5
    .venv/bin/python scripts/fake_slskd.py --scenario stall   # every search plays "stall"

API key: ``--api-key``, else the env var SLSKD_FAKE_API_KEY, else ``releasomat-fake-slskd-key``.
A wrong key gets 401, like slskd.

**Scenarios** (what can go wrong on Soulseek), chosen for every search with ``--scenario NAME``,
or for one search by a word in its search text. The catalog has a song for each word:
"Releasomat Test – stall", "Releasomat Test – wrongfile", …

- ``normal`` (default): several peers per song: FLAC 16 bit, MP3 320, MP3 128, a FLAC 24 bit
  peer without a free upload slot, a live version, an M4A and a locked file.
- ``noresults``: nobody answers.
- ``lowquality``: only MP3 128, MP3 VBR ~190 and M4A 256.
- ``offline``: the best peer ("flacfan") is offline: enqueueing answers 404.
- ``rejected``: the best peer rejects the download ("Completed, Rejected").
- ``stall``: the best peer's transfer stops at 30 % and never moves again.
- ``wrongfile``: the best peer delivers a file a minute longer than its search result says
  (another recording).
- ``slowsearch``: the search takes 30 s (at speed 1) and answers arrive one by one.
- ``exactfails``: a search without a ``*`` wildcard finds nothing; a simpler one with ``*`` does.

The other peers behave normally, so Releasomat should end up with the next best file.
"""

import argparse
import asyncio
import hmac
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.26.1"
DEFAULT_API_KEY = "releasomat-fake-slskd-key"
SCENARIOS = (
    "normal", "noresults", "lowquality", "offline", "rejected", "stall", "wrongfile",
    "slowsearch", "exactfails",
)  # fmt: skip
BEST_PEER = "flacfan"  # the peer the problem scenarios break by default
BUSY = "Only one concurrent operation is permitted. Wait until the previous request completes"

# Base timings in seconds (divided by --speed).
SEARCH_SECONDS = 3.0
SLOW_SEARCH_SECONDS = 30.0
POST_SECONDS = 0.2  # how long POST /searches is busy (a second one meanwhile gets 429)
STEP_SECONDS = 0.5  # Requested -> Queued -> Initializing
TRANSFER_SECONDS = 4.0  # InProgress from 0 to 100 %
MOVE_SECONDS = 0.3  # slskd sets "Completed, Succeeded" before the file is moved into place


# --- The catalog: songs and the files peers share ---------------------------------------------


@dataclass(frozen=True)
class Song:
    artist: str
    title: str
    album: str
    length: int  # seconds
    year: int = 2020
    track: int = 1
    album_artist: str | None = None


EXTRA_SONGS = [
    Song("Bicep", "Glue", "Bicep", 269, 2017, 6),
    Song("deadmau5", "Strobe (Extended Mix)", "For Lack of a Better Name", 637, 2009, 10),
    Song("Röyksopp", "Eple", "Melody A.M.", 221, 2001, 2),
    Song("Fred again..", "Delilah (pull me out of this)", "Actual Life 3", 258, 2022, 5),
    *(Song("Releasomat Test", word, "Scenario Songs", 180, 2026, n) for n, word in
      enumerate(SCENARIOS[1:], start=1)),
]  # fmt: skip


@dataclass
class Offer:
    """One file one peer shares, as it appears in a search response."""

    username: str
    filename: str  # the remote path, with backslashes
    length: int | None  # seconds
    bit_rate: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = 44100
    vbr: bool | None = None
    extension: str = ""  # peers often send nothing here (Explo #202)
    free_slot: bool = True
    queue_length: int = 0
    upload_speed: int = 4_000_000
    locked: bool = False
    real_length: float | None = None  # the length of the file actually delivered

    @property
    def ext(self) -> str:
        return self.filename.rsplit(".", 1)[-1].lower()

    @property
    def size(self) -> int:
        seconds = self.length or 200
        kbit = self.bit_rate or (1411 if self.bit_depth == 24 else 900)
        return int(seconds * kbit * 125)

    def file_json(self) -> dict:
        return {
            "bitDepth": self.bit_depth,
            "bitRate": self.bit_rate,
            "code": 1,
            "extension": self.extension,
            "filename": self.filename,
            "isVariableBitRate": self.vbr,
            "length": self.length,
            "sampleRate": self.sample_rate,
            "size": self.size,
            "isLocked": self.locked,
        }


def _part(text: str) -> str:
    """A folder or file name as a peer would have it (no path separators)."""
    return text.replace("\\", "_").replace("/", "_")


def offers_for(song: Song, scenario: str) -> list[Offer]:
    """The files peers share for one song."""
    a, t, al = _part(song.artist), _part(song.title), _part(song.album)
    folder_artist = _part(song.album_artist or song.artist)
    n, y, length = f"{song.track:02d}", song.year, song.length
    flac = Offer("flacfan", rf"@@flacfan\Music\{folder_artist}\{al} ({y}) [FLAC]\{n} - {t}.flac",
                 length, bit_depth=16, extension="flac")  # fmt: skip
    hires = Offer("vinyl_vault", rf"@@vinyl\Lossless\{folder_artist} - {al} [24-96]\{n}. {t}.flac",
                  length, bit_depth=24, sample_rate=96000, free_slot=False, queue_length=14,
                  upload_speed=900_000)  # fmt: skip
    mp3 = Offer("mp3lover", rf"@@mp3lover\MP3\{folder_artist} - {al}\{n} {a} - {t}.mp3", length,
                bit_rate=320, vbr=False, upload_speed=2_500_000)  # fmt: skip
    low = Offer("lofi_larry", rf"@@larry\shared\{a} - {t}.mp3", length, bit_rate=128, vbr=False,
                upload_speed=300_000)  # fmt: skip
    vbr = Offer("vbr_vic", rf"@@vic\mp3\{a}\{al}\{n} - {t}.mp3", length, bit_rate=190, vbr=True)
    live = Offer("bootleg_bob", rf"@@bob\Bootlegs\{folder_artist} - Live 1999\{n} {t} (Live).flac",
                 length + 40, bit_depth=16, extension="flac")  # fmt: skip
    m4a = Offer("aac_annie", rf"@@annie\iTunes\{folder_artist}\{al}\{n} {t}.m4a", length,
                bit_rate=256, extension="m4a")  # fmt: skip
    locked = Offer("private_pat", rf"@@pat\Buddies only\{a} - {t}.flac", length, bit_depth=16,
                   locked=True)  # fmt: skip
    if scenario == "lowquality":
        return [low, vbr, m4a]
    return [flac, hires, mp3, low, live, m4a, locked]


def load_catalog(library: Path | None) -> list[Song]:
    """The demo library's songs (tags read with mutagen) plus the made-up ones."""
    songs: list[Song] = []
    if library and library.is_dir():
        import mutagen

        for path in sorted(library.rglob("*")):
            if path.suffix.lower() not in (".mp3", ".flac", ".m4a", ".opus", ".ogg"):
                continue
            try:
                audio = mutagen.File(path, easy=True)
            except Exception:
                continue
            if audio is None or not audio.tags:
                continue
            tags = audio.tags

            def tag(name: str, default: str = "", tags=tags) -> str:
                values = tags.get(name) or [default]
                return str(values[0])

            year = "".join(c for c in tag("date", "2020")[:4] if c.isdigit()) or "2020"
            track = "".join(c for c in tag("tracknumber", "1").split("/")[0] if c.isdigit())
            songs.append(
                Song(
                    artist=tag("artist", path.parent.parent.name),
                    title=tag("title", path.stem),
                    album=tag("album", path.parent.name),
                    length=round(audio.info.length),
                    year=int(year),
                    track=int(track or 1),
                    album_artist=tag("albumartist") or None,
                )
            )
    return songs + EXTRA_SONGS


# --- Making the audio files ---------------------------------------------------------------------

AudioMaker = Callable[[Path, str, float], None]

_CODECS = {
    "flac": ["-c:a", "flac", "-sample_fmt", "s16"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
    "aiff": ["-c:a", "pcm_s16be"],
    "aif": ["-c:a", "pcm_s16be"],
    "wav": ["-c:a", "pcm_s16le"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
    "ogg": ["-c:a", "libvorbis"],
    "opus": ["-c:a", "libopus"],
}


def ffmpeg_audio(target: Path, ext: str, seconds: float) -> None:
    """A quiet 440 Hz tone of ``seconds`` length in the format ``ext``, made with ffmpeg."""
    codec = _CODECS.get(ext, _CODECS["flac"])
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=440:sample_rate=44100:duration={max(seconds, 1):.1f}",
        "-af", "volume=0.05", "-ac", "1", *codec, "-f", _ffmpeg_format(ext), str(target),
    ]  # fmt: skip
    subprocess.run(command, check=True, capture_output=True)


def _ffmpeg_format(ext: str) -> str:
    return {"aif": "aiff", "m4a": "ipod", "ogg": "ogg", "opus": "opus"}.get(ext, ext)


# --- The fake slskd -------------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class FakeSearch:
    id: str
    text: str
    token: int
    started: float  # time.monotonic()
    started_at: str
    duration: float
    arrivals: list[tuple[float, list[Offer]]]  # (seconds after start, one peer's files)
    cancelled_after: float | None = None
    ended_at: str | None = None

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def is_complete(self) -> bool:
        return self.cancelled_after is not None or self.elapsed() >= self.duration

    def arrived(self) -> list[list[Offer]]:
        limit = self.cancelled_after if self.cancelled_after is not None else self.elapsed()
        return [files for after, files in self.arrivals if after <= limit]

    def state(self) -> str:
        if self.cancelled_after is not None:
            return "Completed, Cancelled"
        return "Completed, TimedOut" if self.is_complete() else "InProgress"

    def responses_json(self) -> list[dict]:
        out = []
        for files in self.arrived():
            first = files[0]
            unlocked = [f for f in files if not f.locked]
            locked = [f for f in files if f.locked]
            out.append(
                {
                    "fileCount": len(unlocked),
                    "files": [f.file_json() for f in unlocked],
                    "hasFreeUploadSlot": first.free_slot,
                    "lockedFileCount": len(locked),
                    "lockedFiles": [f.file_json() for f in locked],
                    "queueLength": first.queue_length,
                    "token": self.token,
                    "uploadSpeed": first.upload_speed,
                    "username": first.username,
                }
            )
        return out

    def json(self, include_responses: bool = False) -> dict:
        complete = self.is_complete()
        arrived = self.arrived()
        if complete and self.ended_at is None:
            self.ended_at = now_iso()
        return {
            "endedAt": self.ended_at if complete else None,
            "fileCount": sum(1 for files in arrived for f in files if not f.locked),
            "id": self.id,
            "isComplete": complete,
            "lockedFileCount": sum(1 for files in arrived for f in files if f.locked),
            "responseCount": len(arrived),
            # Like slskd: the responses are only kept once the search is complete.
            "responses": self.responses_json() if include_responses and complete else [],
            "searchText": self.text,
            "startedAt": self.started_at,
            "state": self.state(),
            "token": self.token,
        }


@dataclass
class FakeTransfer:
    id: str
    batch_id: str
    username: str
    filename: str
    size: int
    offer: Offer | None
    behaviour: str  # "normal", "rejected", "stall", "wrongfile", "notshared"
    destination: str | None
    state: str = "Requested"
    requested_at: str = field(default_factory=now_iso)
    enqueued_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    bytes_transferred: int = 0
    average_speed: float = 0.0
    exception: str | None = None
    removed: bool = False
    local_filename: str | None = None  # where the file ended up (not part of slskd's Transfer)
    task: asyncio.Task | None = None

    def json(self) -> dict:
        percent = self.bytes_transferred / self.size * 100 if self.size else 0.0
        return {
            "batchId": self.batch_id,
            "id": self.id,
            "username": self.username,
            "direction": "Download",
            "filename": self.filename,
            "size": self.size,
            "state": self.state,
            "requestedAt": self.requested_at,
            "enqueuedAt": self.enqueued_at,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "bytesTransferred": self.bytes_transferred,
            "averageSpeed": self.average_speed,
            "placeInQueue": None,
            "exception": self.exception,
            "attempts": 0,
            "nextAttemptAt": None,
            "removed": self.removed,
            "bytesRemaining": self.size - self.bytes_transferred,
            "elapsedTime": None,
            "percentComplete": percent,
            "remainingTime": None,
        }


@dataclass
class FakeBatch:
    id: str
    search_id: str | None
    username: str
    destination: str | None
    created_at: str = field(default_factory=now_iso)
    transfer_ids: list[str] = field(default_factory=list)


class FakeSlskd:
    """The fake's state and its web app (``.app``). Tests talk to the app in-process with
    ``httpx.ASGITransport``; ``main()`` serves it with uvicorn."""

    def __init__(
        self,
        songs: list[Song],
        downloads_dir: Path,
        api_key: str = DEFAULT_API_KEY,
        *,
        speed: float = 1.0,
        scenario: str | None = None,
        failing_peers: set[str] | None = None,
        make_audio: AudioMaker = ffmpeg_audio,
        logged_in: bool = True,
        version: str = VERSION,
    ):
        if scenario and scenario not in SCENARIOS:
            raise ValueError(f"Unknown scenario {scenario!r}; one of: {', '.join(SCENARIOS)}")
        self.songs = songs
        self.downloads_dir = Path(downloads_dir)
        self.api_key = api_key
        self.speed = speed
        self.scenario = scenario
        self.failing_peers = failing_peers if failing_peers is not None else {BEST_PEER}
        self.make_audio = make_audio
        # Like slskd's "incomplete" folder: files are made here, then moved into downloads_dir.
        self.incomplete_dir = Path(tempfile.mkdtemp(prefix="fake-slskd-incomplete-"))
        self.logged_in = logged_in
        self.version = version
        self.searches: dict[str, FakeSearch] = {}
        self.transfers: dict[str, FakeTransfer] = {}
        self.batches: dict[str, FakeBatch] = {}
        self.history: list[FakeSearch] = []  # every search, in order (tests look at them)
        self.overlaps = 0  # searches started while another one was still running
        self._posting_search = False
        self._enqueues = 0
        self._next_token = 1000
        self.app = self._make_app()

    # --- helpers ---

    def seconds(self, base: float) -> float:
        return base / self.speed

    def scenario_for(self, text: str) -> str:
        """The scenario for a search text or a shared file's path: ``--scenario``, else a
        scenario word in it ("Releasomat Test – stall"), else "normal"."""
        if self.scenario:
            return self.scenario
        words = set("".join(c if c.isalnum() else " " for c in text.casefold()).split())
        return next((s for s in SCENARIOS[1:] if s in words), "normal")

    def find_offers(self, text: str, scenario: str) -> list[Offer]:
        """Every shared file whose path contains all words of the search (a leading ``*`` is
        a wildcard, as on Soulseek: ``*aft`` matches "Daft")."""
        tokens = [t.lstrip("*") for t in text.casefold().split() if t.lstrip("*")]
        found = []
        for song in self.songs:
            for offer in offers_for(song, scenario):
                path = offer.filename.casefold()
                if all(token in path for token in tokens):
                    found.append(offer)
        return found

    def find_offer(self, username: str, filename: str) -> Offer | None:
        scenario = self.scenario or "normal"
        for song in self.songs:
            for offer in offers_for(song, scenario) + offers_for(song, "lowquality"):
                if offer.username == username and offer.filename == filename:
                    return offer
        return None

    async def close(self) -> None:
        """Stop the transfers that are still "running" (tests call this at the end)."""
        tasks = [t.task for t in self.transfers.values() if t.task and not t.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        shutil.rmtree(self.incomplete_dir, ignore_errors=True)

    # --- searches ---

    @property
    def search_texts(self) -> list[str]:
        return [search.text for search in self.history]

    def start_search(self, search_id: str, text: str) -> FakeSearch:
        if any(not search.is_complete() for search in self.searches.values()):
            self.overlaps += 1
        scenario = self.scenario_for(text)
        offers: list[Offer] = []
        if scenario != "noresults" and not (scenario == "exactfails" and "*" not in text):
            offers = self.find_offers(text, scenario)
        by_peer: dict[str, list[Offer]] = {}
        for offer in offers:
            by_peer.setdefault(offer.username, []).append(offer)
        duration = self.seconds(SLOW_SEARCH_SECONDS if scenario == "slowsearch" else SEARCH_SECONDS)
        count = len(by_peer)
        arrivals = [
            (duration * (i + 1) / (count + 1), files) for i, files in enumerate(by_peer.values())
        ]
        self._next_token += 1
        search = FakeSearch(
            id=search_id,
            text=text,
            token=self._next_token,
            started=time.monotonic(),
            started_at=now_iso(),
            duration=duration,
            arrivals=arrivals,
        )
        self.searches[search_id] = search
        self.history.append(search)
        return search

    # --- transfers ---

    def behaviour_for(self, username: str, offer: Offer | None, scenario: str) -> str:
        if offer is None:
            return "notshared"
        if username in self.failing_peers and scenario in ("rejected", "stall", "wrongfile"):
            return scenario
        return "normal"

    async def run_transfer(self, transfer: FakeTransfer) -> None:
        """Go through slskd's states; at the end write the file into the downloads folder."""
        await asyncio.sleep(self.seconds(STEP_SECONDS))
        if transfer.behaviour in ("rejected", "notshared"):
            transfer.state = "Completed, Rejected"
            transfer.exception = "File not shared."
            transfer.ended_at = now_iso()
            return
        transfer.state = "Queued, Remotely"
        transfer.enqueued_at = now_iso()
        await asyncio.sleep(self.seconds(STEP_SECONDS))
        transfer.state = "Initializing"
        offer = transfer.offer
        length = offer.length if offer and offer.length else 200
        real = length + 60 if transfer.behaviour == "wrongfile" else length
        if offer and offer.real_length:
            real = offer.real_length
        ext = transfer.filename.rsplit(".", 1)[-1].lower()
        self.incomplete_dir.mkdir(parents=True, exist_ok=True)
        incomplete = self.incomplete_dir / f"{transfer.id}.{ext}"
        making = asyncio.create_task(asyncio.to_thread(self.make_audio, incomplete, ext, real))
        await asyncio.sleep(self.seconds(STEP_SECONDS))
        transfer.state = "InProgress"
        transfer.started_at = now_iso()
        steps = 10
        for step in range(1, steps + 1):
            await asyncio.sleep(self.seconds(TRANSFER_SECONDS) / steps)
            if transfer.behaviour == "stall" and step > 3:
                await asyncio.Event().wait()  # stuck at 30 % until cancelled
            transfer.bytes_transferred = int(transfer.size * step / steps)
            elapsed = self.seconds(TRANSFER_SECONDS) * step / steps
            transfer.average_speed = transfer.bytes_transferred / max(elapsed, 0.001)
        try:
            await making
        except Exception as exc:  # ffmpeg missing or failed: an error like slskd's
            transfer.state = "Completed, Errored"
            transfer.exception = f"Fake slskd couldn't make the file: {exc}"
            transfer.ended_at = now_iso()
            incomplete.unlink(missing_ok=True)
            return
        transfer.state = "Completed, Succeeded"
        transfer.ended_at = now_iso()
        await asyncio.sleep(self.seconds(MOVE_SECONDS))
        transfer.local_filename = str(self.move_into_place(transfer, incomplete))

    def move_into_place(self, transfer: FakeTransfer, incomplete: Path) -> Path:
        """Like slskd: into <downloads>/<batch destination or remote parent folder>/<file name>;
        an existing name gets "_<ticks>" (slskd's default ``exists: rename``)."""
        parts = transfer.filename.replace("/", "\\").split("\\")
        name = _part(parts[-1])
        folder = transfer.destination or (_part(parts[-2]) if len(parts) > 1 else "")
        target_dir = self.downloads_dir / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        while target.exists():
            ticks = int(time.time() * 10_000_000)
            target = target_dir / f"{Path(name).stem}_{ticks}{Path(name).suffix}"
        shutil.move(incomplete, target)
        return target

    def cancel(self, transfer: FakeTransfer) -> None:
        """Like slskd: cancelling a finished transfer changes nothing (its file is still moved
        into place)."""
        if transfer.state.startswith("Completed"):
            return
        if transfer.task and not transfer.task.done():
            transfer.task.cancel()
        transfer.state = "Completed, Cancelled"
        transfer.exception = "Operation cancelled"
        transfer.ended_at = now_iso()
        for leftover in self.incomplete_dir.glob(f"{transfer.id}.*"):
            leftover.unlink(missing_ok=True)

    def batch_json(self, batch: FakeBatch) -> dict:
        return {
            "searchId": batch.search_id,
            "id": batch.id,
            "username": batch.username,
            "direction": "Download",
            "createdAt": batch.created_at,
            "transfers": [self.transfers[t].json() for t in batch.transfer_ids],
            # slskd 0.26.1 keeps only the destination (the external ID isn't stored).
            "options": {"destination": batch.destination, "externalId": None},
        }

    # --- the web app ---

    def _make_app(self) -> FastAPI:
        fake = self

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            yield
            await fake.close()  # when the server stops: no "running" transfers, no temp folder

        app = FastAPI(
            title="Fake slskd (Releasomat development)",
            docs_url=None,
            redoc_url=None,
            lifespan=lifespan,
        )

        @app.middleware("http")
        async def check_api_key(request: Request, call_next):
            if request.url.path.startswith("/api/"):
                key = request.headers.get("x-api-key", "")
                if not hmac.compare_digest(key.encode(), fake.api_key.encode()):
                    return Response(status_code=401)
            return await call_next(request)

        @app.get("/", response_class=PlainTextResponse)
        async def home():
            return (
                f"Fake slskd {fake.version} for Releasomat development: no Soulseek network, "
                f"{len(fake.songs)} songs, scenario {fake.scenario or 'by search words'}.\n"
            )

        def state_json() -> dict:
            state = "Connected, LoggedIn" if fake.logged_in else "Disconnected"
            return {
                "address": "vps.slsknet.org" if fake.logged_in else None,
                "ipEndPoint": "127.0.0.1:2271" if fake.logged_in else None,
                "state": state,
                "isConnected": fake.logged_in,
                "isConnecting": False,
                "isLoggedIn": fake.logged_in,
                "isLoggingIn": False,
                "isTransitioning": False,
            }

        @app.get("/api/v0/application")
        async def application():
            return {
                "version": {
                    "full": f"{fake.version}.0+fake",
                    "current": fake.version,
                    "latest": None,
                    "isUpdateAvailable": None,
                    "isCanary": False,
                    "isDevelopment": False,
                },
                "pendingReconnect": False,
                "pendingRestart": False,
                "server": state_json(),
                "user": {
                    "username": "releasomat_fake" if fake.logged_in else None,
                    "privileges": {"isPrivileged": False, "privilegesRemaining": 0},
                    "statistics": {
                        "averageSpeed": 0,
                        "directoryCount": 0,
                        "fileCount": 0,
                        "uploadCount": 0,
                    },
                },
                "shares": {"ready": True, "directories": 0, "files": 0},
            }

        @app.get("/api/v0/application/version")
        async def application_version():
            return fake.version

        @app.get("/api/v0/server")
        async def server():
            return state_json()

        # Searches

        @app.post("/api/v0/searches")
        async def post_search(request: Request):
            body = await request.json()
            text = (body or {}).get("searchText") or ""
            if not text.strip():
                message = (
                    "The field SearchText can not be null, empty, or consist of only whitespace"
                )
                return JSONResponse(message, status_code=400)
            if fake._posting_search:
                return JSONResponse(BUSY, status_code=429)
            fake._posting_search = True
            try:
                await asyncio.sleep(fake.seconds(POST_SECONDS))
                if not fake.logged_in:
                    message = "The server connection must be connected and logged in"
                    return JSONResponse(message, status_code=409)
                search_id = body.get("id") or str(uuid.uuid4())
                if search_id in fake.searches:
                    return JSONResponse(f"Search {search_id} already exists", status_code=400)
                return fake.start_search(search_id, text).json()
            finally:
                fake._posting_search = False

        @app.get("/api/v0/searches")
        async def list_searches():
            return [s.json() for s in fake.searches.values()]

        @app.get("/api/v0/searches/{search_id}")
        async def get_search(search_id: str, includeResponses: bool = False):
            search = fake.searches.get(search_id)
            if search is None:
                return Response(status_code=404)
            return search.json(include_responses=includeResponses)

        @app.get("/api/v0/searches/{search_id}/responses")
        async def get_responses(search_id: str):
            search = fake.searches.get(search_id)
            if search is None:
                return Response(status_code=404)
            return search.responses_json() if search.is_complete() else []

        @app.put("/api/v0/searches/{search_id}")
        async def stop_search(search_id: str):
            search = fake.searches.get(search_id)
            if search is None:
                return Response(status_code=404)
            if not search.is_complete():
                search.cancelled_after = search.elapsed()
            return Response(status_code=200)

        @app.delete("/api/v0/searches/{search_id}")
        async def delete_search(search_id: str):
            if fake.searches.pop(search_id, None) is None:
                return Response(status_code=404)
            return Response(status_code=204)

        # Transfers

        @app.post("/api/v0/transfers/downloads/batches")
        async def enqueue_batch(request: Request):
            body = await request.json() or {}
            username = (body.get("username") or "").strip()
            items = body.get("files") or []
            if not username:
                return JSONResponse("The Username field is required.", status_code=400)
            if not items or any(not (i or {}).get("filename") for i in items):
                return JSONResponse("The Files field is required.", status_code=400)
            names = [i["filename"] for i in items]
            if len(set(names)) != len(names):
                return JSONResponse("Two or more files in the request are repeated", 400)
            batch_id = body.get("id") or str(uuid.uuid4())
            try:
                uuid.UUID(batch_id)
                if body.get("searchId"):
                    uuid.UUID(body["searchId"])
            except ValueError:
                message = "One or more provided identifiers is not a valid GUID/UUID"
                return JSONResponse(message, status_code=400)
            if fake._enqueues >= 2:
                return JSONResponse(BUSY, status_code=429)
            fake._enqueues += 1
            try:
                await asyncio.sleep(fake.seconds(0.1))
                # The scenario comes from --scenario or the song's path ("… - offline.flac").
                scenario = fake.scenario_for(" ".join(names))
                if scenario == "offline" and username in fake.failing_peers:
                    return JSONResponse(f"User {username} appears to be offline", status_code=404)
                if batch_id in fake.batches:
                    return JSONResponse(f"A batch with ID {batch_id} already exists", 409)
                destination = (body.get("options") or {}).get("destination")
                batch = FakeBatch(batch_id, body.get("searchId"), username, destination)
                fake.batches[batch_id] = batch
                failures = []
                for item in items:
                    busy = any(
                        t.username == username
                        and t.filename == item["filename"]
                        and not t.state.startswith("Completed")
                        for t in fake.transfers.values()
                    )
                    if busy:
                        failures.append(
                            {"filename": item["filename"], "message": "Already in progress"}
                        )
                        continue
                    offer = fake.find_offer(username, item["filename"])
                    scenario = fake.scenario_for(item["filename"])
                    transfer = FakeTransfer(
                        id=str(uuid.uuid4()),
                        batch_id=batch_id,
                        username=username,
                        filename=item["filename"],
                        size=int(item.get("size") or (offer.size if offer else 0)),
                        offer=offer,
                        behaviour=fake.behaviour_for(username, offer, scenario),
                        destination=destination,
                    )
                    fake.transfers[transfer.id] = transfer
                    batch.transfer_ids.append(transfer.id)
                    transfer.task = asyncio.create_task(fake.run_transfer(transfer))
                answer = {"batch": fake.batch_json(batch), "failures": failures}
                status = 200 if len(failures) == len(items) else 207 if failures else 201
                return JSONResponse(answer, status_code=status)
            finally:
                fake._enqueues -= 1

        @app.get("/api/v0/transfers/downloads")
        async def list_downloads(includeRemoved: bool = False):
            users: dict[str, dict[str, list[dict]]] = {}
            for t in fake.transfers.values():
                if t.removed and not includeRemoved:
                    continue
                directory = t.filename.replace("/", "\\").rsplit("\\", 1)[0]
                users.setdefault(t.username, {}).setdefault(directory, []).append(t.json())
            return [
                {
                    "username": user,
                    "directories": [
                        {"directory": d, "fileCount": len(files), "files": files}
                        for d, files in directories.items()
                    ],
                }
                for user, directories in users.items()
            ]

        @app.get("/api/v0/transfers/downloads/batches/{batch_id}")
        async def get_batch(batch_id: str):
            try:
                uuid.UUID(batch_id)
            except ValueError:
                return JSONResponse(f"The specified id {batch_id} is not a valid GUID/UUID", 400)
            batch = fake.batches.get(batch_id)
            if batch is None:
                return Response(status_code=404)
            return fake.batch_json(batch)

        @app.get("/api/v0/transfers/downloads/{username}/{transfer_id}")
        async def get_download(username: str, transfer_id: str):
            transfer = fake.transfers.get(transfer_id)
            if transfer is None:
                return Response(status_code=404)
            return transfer.json()

        @app.delete("/api/v0/transfers/downloads/{username}/{transfer_id}")
        async def cancel_download(username: str, transfer_id: str, remove: bool = False):
            transfer = fake.transfers.get(transfer_id)
            if transfer is None:
                return Response(status_code=404)
            fake.cancel(transfer)
            if remove:
                transfer.removed = True
            return Response(status_code=204)

        return app


# --- Running it ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=5030)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=ROOT / "dev" / "slskd",
        help="where finished downloads land (slskd's downloads folder; default dev/slskd)",
    )
    parser.add_argument(
        "--library",
        type=Path,
        default=ROOT / "dev" / "library" / "Demo",
        help="the demo library whose songs the fake peers share",
    )
    parser.add_argument("--api-key", help="default: $SLSKD_FAKE_API_KEY or the dev key")
    parser.add_argument(
        "--speed", type=float, default=1.0, help="2 = everything twice as fast, 0.5 = slower"
    )
    parser.add_argument("--scenario", choices=SCENARIOS, help="play this for every search")
    parser.add_argument("--logged-out", action="store_true", help="act as if not logged in")
    args = parser.parse_args(argv)
    if shutil.which("ffmpeg") is None:
        parser.error("ffmpeg is needed to make the audio files.")
    api_key = args.api_key or os.environ.get("SLSKD_FAKE_API_KEY") or DEFAULT_API_KEY
    args.downloads_dir.mkdir(parents=True, exist_ok=True)
    songs = load_catalog(args.library)
    fake = FakeSlskd(
        songs,
        args.downloads_dir,
        api_key,
        speed=args.speed,
        scenario=args.scenario,
        logged_in=not args.logged_out,
    )
    where = "the dev key" if api_key == DEFAULT_API_KEY else "your key"
    print(
        f"Fake slskd {VERSION} on http://{args.host}:{args.port} (API key: {where}), "
        f"{len(songs)} songs, files into {args.downloads_dir}. No Soulseek network involved.",
        flush=True,
    )
    if api_key == DEFAULT_API_KEY:
        print(f"Dev API key: {DEFAULT_API_KEY}", flush=True)

    import uvicorn

    uvicorn.run(fake.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
