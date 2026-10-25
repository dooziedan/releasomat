"""The only place that talks to Navidrome, through the Subsonic API it implements (plus two calls
of Navidrome's own API, see ``songs_by_path`` and ``song_info``).

Logging in: Subsonic clients never send the password itself. Each request carries a random
"salt" and ``md5(password + salt)`` as token (``u``, ``t``, ``s`` parameters), so a captured
request can't be reused with another salt. The password is never logged, and error messages
never include the request address or body (they carry the token). httpx would log every request
address at INFO level; ``app/main.py`` turns its logger down to WARNING for that reason.

What Releasomat does here (docs/architecture.md, "Navidrome details"):

- ``search`` songs (an MBID as query matches exactly), ``music_folders``;
- ``find_in_library``: the song that is a wanted track; ``find_elsewhere``: the same, but only
  outside Releasomat's folder, checked with ``song_info`` (Navidrome's own API: real path and
  whether the file is missing), for files that left the folder (ADR 0009);
- ``scan``: wait until no scan runs, start one for Releasomat's folder only, wait until it is done
  (Navidrome ignores a scan request while another scan runs);
- ``find_download``: the Navidrome song of a file Releasomat just saved: by MBID, else by its path
  through Navidrome's own API, else by name and length;
- playlists: ``create_playlist``, ``replace_playlist`` (keeps the ID), ``get_playlist``,
  ``delete_playlist``; long song lists go as a form POST.
"""

import asyncio
import hashlib
import logging
import secrets
import time
from collections.abc import Collection
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from sqlmodel import Session

from app import httpclient, matching
from app import settings as user_settings

log = logging.getLogger(__name__)

API_VERSION = "1.16.1"  # the Subsonic API version Navidrome implements
CLIENT_NAME = "releasomat"
SCAN_TIMEOUT = 600  # seconds to wait for a scan
SCAN_POLL = 1.0  # seconds between two "are you done?" questions
ELSEWHERE_TRIES = 3  # matches find_elsewhere checks before it gives up

# Subsonic error codes worth explaining in plain words.
ERRORS = {
    40: "Wrong user name or password.",
    41: "Navidrome doesn't accept token logins for this user.",
    50: "This Navidrome user may not do that (admin rights needed).",
}
NOT_FOUND = 70


class NavidromeError(Exception):
    """Navidrome could not be reached or refused a request. The message is safe to show;
    ``details`` (which call, HTTP status, Subsonic error code, a short excerpt of the answer) go
    into the error report. Neither ever contains the password, token or salt."""

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = details

    @property
    def code(self) -> int | None:
        code = self.details.get("code")
        return code if isinstance(code, int) else None


@dataclass
class PingResult:
    ok: bool
    message: str
    server_version: str | None = None


@dataclass(frozen=True)
class FoundSong:
    """A downloaded file's song in Navidrome, and how it was found ("mbid", "path", "name")."""

    id: str
    method: str


def _excerpt(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


class Navidrome:
    """A Navidrome server. ``http`` lets tests pass a client; normally one is made per call."""

    def __init__(
        self,
        url: str,
        user: str,
        password: str,
        *,
        http: httpx.AsyncClient | None = None,
        subfolder: str = "Releasomat",
    ):
        self.url = url.strip().rstrip("/")
        self.user = user
        self._password = password
        self._http = http
        self.subfolder = subfolder.strip().strip("/")
        self._native_token: str | None = None

    def __repr__(self) -> str:  # never shows the password
        return f"Navidrome({self.url!r}, user={self.user!r})"

    @classmethod
    def from_settings(cls, session: Session, **kwargs) -> "Navidrome":
        get = user_settings.get
        return cls(
            get(session, "NAVIDROME_URL"),
            get(session, "NAVIDROME_USER"),
            get(session, "NAVIDROME_PASSWORD"),
            subfolder=get(session, "NAVIDROME_SUBFOLDER"),
            **kwargs,
        )

    @property
    def configured(self) -> bool:
        return bool(self.url and self.user and self._password)

    @property
    def secrets(self) -> list[str]:
        """Values the error report scrubber must remove (app/reports.py)."""
        return [s for s in (self._password, self._native_token) if s]

    def auth_params(self, salt: str | None = None) -> dict[str, str]:
        """The login parameters of every Subsonic request: token and salt, never the password."""
        salt = salt or secrets.token_hex(8)
        # MD5 is what the Subsonic API prescribes; the salt makes each token single-purpose.
        token = hashlib.md5((self._password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.user,
            "t": token,
            "s": salt,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }

    # --- Talking to Navidrome -----------------------------------------------------------------

    async def _send(self, method: str, address: str, call: str, **kwargs) -> httpx.Response:
        """One HTTP request with plain-language errors. ``call`` names it in error reports."""
        if not self.configured:
            raise NavidromeError(
                "Enter Navidrome's address, a user and its password first (Settings).", call=call
            )
        if not self.url.startswith(("http://", "https://")):
            raise NavidromeError("The address must start with http:// or https://.", call=call)
        try:
            if self._http is not None:
                return await self._http.request(method, address, **kwargs)
            async with httpclient.client() as http:
                return await http.request(method, address, **kwargs)
        except httpx.TimeoutException:
            raise NavidromeError(f"No answer from {self.url} in time.", call=call) from None
        except httpx.HTTPError as exc:
            reason = type(exc).__name__
            raise NavidromeError(
                f"Can't reach {self.url} ({reason}). Check the address.", call=call
            ) from None

    async def _call(self, endpoint: str, params: dict | None = None, *, post: bool = False) -> dict:
        """One Subsonic request. Returns the inner "subsonic-response"; raises NavidromeError.
        ``params`` values may be lists (sent as repeated parameters, e.g. ``songId``).
        ``post=True`` sends everything as a form (OpenSubsonic ``formPost``): for long lists."""
        address = f"{self.url}/rest/{endpoint}"
        query = {**self.auth_params(), **(params or {})}
        if post:
            response = await self._send("POST", address, endpoint, data=query)
        else:
            response = await self._send("GET", address, endpoint, params=query)
        if response.status_code != 200:
            raise NavidromeError(
                f"{self.url} answered with HTTP {response.status_code}.",
                call=endpoint,
                http_status=response.status_code,
                excerpt=_excerpt(response.text),
            )
        try:
            body = response.json()["subsonic-response"]
        except (ValueError, KeyError, TypeError):
            raise NavidromeError(
                f"{self.url} doesn't answer like Navidrome. Is it the right address and port?",
                call=endpoint,
                http_status=response.status_code,
                excerpt=_excerpt(response.text),
            ) from None
        if body.get("status") != "ok":
            error = body.get("error") or {}
            code = error.get("code")
            message = ERRORS.get(code) or error.get("message") or "Navidrome refused."
            raise NavidromeError(
                message, call=endpoint, code=code, excerpt=_excerpt(error.get("message") or "")
            )
        return body

    async def ping(self) -> PingResult:
        """Check address and login (the Settings page's Test button)."""
        try:
            body = await self._call("ping")
        except NavidromeError as exc:
            return PingResult(False, str(exc))
        version = body.get("serverVersion")
        name = "Navidrome" if body.get("type", "navidrome") == "navidrome" else body.get("type")
        shown = f" {version}" if version else ""
        return PingResult(True, f"Connected to {name}{shown} as {self.user}.", version)

    # --- Library ------------------------------------------------------------------------------

    async def music_folders(self) -> list[dict]:
        """The libraries this user can see: ``[{"id": 1, "name": "Music Library"}]``."""
        body = await self._call("getMusicFolders")
        return list((body.get("musicFolders") or {}).get("musicFolder") or [])

    async def search(self, query: str, count: int = 20) -> list[dict]:
        """Songs matching ``query`` (``search3``). A MusicBrainz ID as query matches exactly."""
        body = await self._call(
            "search3",
            {"query": query, "songCount": str(count), "artistCount": "0", "albumCount": "0"},
        )
        return list((body.get("searchResult3") or {}).get("song") or [])

    async def find_in_library(
        self, track: matching.Wanted, threshold: int, skip_ids: Collection[str] = ()
    ) -> matching.SongMatch | None:
        """The song in Navidrome that is this track, or None: by MusicBrainz ID, then by name
        (comparing ISRCs, names and lengths, see ``matching.match_song``).

        ``skip_ids`` are songs that were files in Releasomat's folder which have left it: until
        its next scan Navidrome still lists them. Such a song is only the answer when Navidrome's
        own API says it is now elsewhere in the library (Navidrome keeps a song's ID when it
        notices a file was moved)."""
        skip = set(skip_ids)
        if track.mbid:
            songs = {s["id"]: s for s in await self.search(track.mbid, 5) if s.get("id")}
            found = await self._pick(track, songs, threshold, check_all=False, skip_ids=skip)
            if found:
                return found
        songs = {}
        for query in self._name_queries(track):
            for song in await self.search(query, 50):
                if song.get("id"):
                    songs.setdefault(song["id"], song)
            found = await self._pick(track, songs, threshold, check_all=False, skip_ids=skip)
            if found:
                return found
        return None

    @staticmethod
    def _name_queries(track: matching.Wanted) -> list[str]:
        """What to search for by name: main artist and song, then the song alone."""
        song_only = matching.title_parts(track.title).song
        queries = [matching.search_text(track.artist, track.title), song_only]
        return [q for q in dict.fromkeys(queries) if q]

    async def find_elsewhere(
        self, track: matching.Wanted, threshold: int, skip_ids: Collection[str] = ()
    ) -> matching.SongMatch | None:
        """The song in the library **outside Releasomat's folder** that is this track, or None
        (ADR 0009: a file that left the folder, an adopted or a "Don't like" track).

        Like ``find_in_library``, but every match is checked with ``song_info`` (Navidrome's
        own API): songs whose real path is in Releasomat's folder (``<subfolder>/…``) and songs
        Navidrome marks missing are passed over, up to ``ELSEWHERE_TRIES`` matches. If
        Navidrome's own API doesn't work, only ``skip_ids`` (the old songs of files that left
        the folder) are passed over."""
        songs: dict[str, dict] = {}
        if track.mbid:
            for song in await self.search(track.mbid, 5):
                if song.get("id"):
                    songs.setdefault(song["id"], song)
        for query in self._name_queries(track):
            for song in await self.search(query, 50):
                if song.get("id"):
                    songs.setdefault(song["id"], song)
        return await self._pick(track, songs, threshold, check_all=True, skip_ids=set(skip_ids))

    async def _pick(
        self,
        track: matching.Wanted,
        songs: dict[str, dict],
        threshold: int,
        *,
        check_all: bool,
        skip_ids: set[str],
    ) -> matching.SongMatch | None:
        """The best match among ``songs`` whose real place is fine: every match when
        ``check_all``, else only those in ``skip_ids`` are checked with ``song_info`` (outside
        Releasomat's folder and not missing). Up to ``ELSEWHERE_TRIES`` checked matches."""
        passed_over: set[str] = set()
        for _ in range(ELSEWHERE_TRIES):
            candidates = [s for i, s in songs.items() if i not in passed_over]
            found = matching.match_song(track, candidates, threshold)
            if found is None:
                return None
            song_id = found.song["id"]
            if not check_all and song_id not in skip_ids:
                return found
            try:
                info = await self.song_info(song_id)
            except NavidromeError as exc:
                log.info("Can't check where song %s is (%s)", song_id, exc)
                if song_id not in skip_ids:
                    return found
                passed_over.add(song_id)
                continue
            if self.outside_area(info):
                return found
            passed_over.add(song_id)
        return None

    def outside_area(self, info: dict | None) -> bool:
        """Whether a song (``song_info``) is a file in the library outside Releasomat's folder
        that Navidrome hasn't marked missing."""
        if not info or info.get("missing"):
            return False
        path = info.get("path") or ""
        return not (self.subfolder and path.startswith(f"{self.subfolder}/"))

    # --- Scans --------------------------------------------------------------------------------

    async def scan_status(self) -> dict:
        body = await self._call("getScanStatus")
        return body.get("scanStatus") or {}

    async def _wait_for_scan(self, deadline: float, poll: float) -> dict:
        while True:
            status = await self.scan_status()
            if not status.get("scanning"):
                return status
            if time.monotonic() > deadline:
                raise NavidromeError(
                    "Navidrome's scan didn't finish in time. The new songs show up after the "
                    "next refresh.",
                    call="getScanStatus",
                )
            await asyncio.sleep(poll)

    async def scan(
        self, *, timeout: float = SCAN_TIMEOUT, poll: float = SCAN_POLL, full: bool = False
    ) -> str:
        """Let Navidrome scan Releasomat's folder (``startScan`` with ``target=<library>:<folder>``,
        Navidrome 0.59+) and wait until it is done. Falls back to a scan of the whole library
        when the targeted one is refused. Returns "folder" or "library"."""
        deadline = time.monotonic() + timeout
        await self._wait_for_scan(deadline, poll)  # a running scan would ignore our request
        params: dict = {"fullScan": "false"}
        kind = "library"
        if self.subfolder and not full:
            folders = await self.music_folders()
            params["target"] = [f"{f['id']}:{self.subfolder}" for f in folders if "id" in f]
            kind = "folder" if params["target"] else "library"
        try:
            before = await self._call("startScan", params)
        except NavidromeError as exc:
            if exc.code == 50 or "target" not in params:
                raise
            log.info("Navidrome refused a targeted scan (%s); scanning the whole library", exc)
            params.pop("target")
            kind = "library"
            before = await self._call("startScan", params)
        started_at = (before.get("scanStatus") or {}).get("lastScan")
        await asyncio.sleep(min(poll, 0.5))
        status = await self._wait_for_scan(deadline, poll)
        log.debug("Navidrome scan done (%s, last scan %s -> %s)", kind, started_at, status)
        return kind

    # --- Finding a file Releasomat just saved -------------------------------------------------

    async def _login_native(self) -> str:
        """A token for Navidrome's own API (the one its web page uses). Kept for this object."""
        if self._native_token:
            return self._native_token
        response = await self._send(
            "POST",
            f"{self.url}/auth/login",
            "auth/login",
            json={"username": self.user, "password": self._password},
        )
        if response.status_code != 200:
            raise NavidromeError(
                "Navidrome's own login was refused.",
                call="auth/login",
                http_status=response.status_code,
            )
        try:
            self._native_token = response.json()["token"]
        except (ValueError, KeyError, TypeError):
            raise NavidromeError(
                "Navidrome's login answered unexpectedly.", call="auth/login"
            ) from None
        return self._native_token

    async def songs_by_path(self, path: str) -> list[dict]:
        """Songs whose path (relative to its library) starts with ``path``, through Navidrome's
        own API (``GET /api/song?path=``; not part of the stable Subsonic API)."""
        token = await self._login_native()
        response = await self._send(
            "GET",
            f"{self.url}/api/song",
            "api/song",
            params={"path": path, "_start": "0", "_end": "10"},
            headers={"X-ND-Authorization": f"Bearer {token}"},
        )
        if response.status_code != 200:
            raise NavidromeError(
                f"Navidrome's song list answered with HTTP {response.status_code}.",
                call="api/song",
                http_status=response.status_code,
            )
        try:
            songs = response.json()
        except ValueError:
            raise NavidromeError(
                "Navidrome's song list answered unexpectedly.", call="api/song"
            ) from None
        return songs if isinstance(songs, list) else []

    async def song_info(self, song_id: str) -> dict | None:
        """One song as Navidrome's own API knows it (``GET /api/song/<id>``): its real ``path``
        (relative to its library, e.g. ``"Demo/Queen/…/01 Song.mp3"``), ``libraryPath`` and
        ``missing`` (the file is gone since the last scan). None when Navidrome doesn't know
        the ID. The Subsonic API can't tell this: its ``path`` is made up from the tags."""
        token = await self._login_native()
        response = await self._send(
            "GET",
            f"{self.url}/api/song/{quote(song_id, safe='')}",
            "api/song/{id}",
            headers={"X-ND-Authorization": f"Bearer {token}"},
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise NavidromeError(
                f"Navidrome's song details answered with HTTP {response.status_code}.",
                call="api/song/{id}",
                http_status=response.status_code,
            )
        try:
            song = response.json()
        except ValueError:
            raise NavidromeError(
                "Navidrome's song details answered unexpectedly.", call="api/song/{id}"
            ) from None
        if not isinstance(song, dict):
            raise NavidromeError(
                "Navidrome's song details answered unexpectedly.", call="api/song/{id}"
            )
        return song

    async def find_download(
        self, track: matching.Wanted, relative_path: str, threshold: int
    ) -> FoundSong | None:
        """The Navidrome song of a file Releasomat saved at ``relative_path`` (inside MUSIC_DIR),
        after a scan: by the MusicBrainz ID written into it, else by its path (Navidrome's own
        API), else by name and length. None when Navidrome doesn't know it (yet)."""
        if track.mbid:
            for song in await self.search(track.mbid, 5):
                if (song.get("musicBrainzId") or "").lower() == track.mbid.lower():
                    return FoundSong(song["id"], "mbid")
        library_path = f"{self.subfolder}/{relative_path}" if self.subfolder else relative_path
        try:
            for song in await self.songs_by_path(library_path):
                if song.get("path") == library_path and song.get("id"):
                    return FoundSong(song["id"], "path")
        except NavidromeError as exc:
            log.info("Finding %s by path didn't work (%s); trying by name", relative_path, exc)
        found = await self.find_in_library(track, threshold)
        return FoundSong(found.song["id"], "name") if found else None

    # --- Playlists ----------------------------------------------------------------------------

    async def get_playlist(self, playlist_id: str) -> dict | None:
        """A playlist with its songs (``entry``), or None if it doesn't exist (any more)."""
        try:
            body = await self._call("getPlaylist", {"id": playlist_id})
        except NavidromeError as exc:
            if exc.code == NOT_FOUND:
                return None
            raise
        return body.get("playlist") or None

    async def create_playlist(self, name: str, song_ids: list[str]) -> str:
        """A new playlist with these songs in this order; returns its ID."""
        body = await self._call("createPlaylist", {"name": name, "songId": song_ids}, post=True)
        playlist_id = (body.get("playlist") or {}).get("id")
        if not playlist_id:
            raise NavidromeError(
                "Navidrome created the playlist but didn't say its ID.", call="createPlaylist"
            )
        return str(playlist_id)

    async def replace_playlist(self, playlist_id: str, song_ids: list[str]) -> None:
        """Replace all songs of a playlist, keeping its ID (apps that sync playlists don't see a
        new one): ``createPlaylist`` with ``playlistId``."""
        await self._call(
            "createPlaylist", {"playlistId": playlist_id, "songId": song_ids}, post=True
        )

    async def update_playlist(
        self, playlist_id: str, *, comment: str | None = None, name: str | None = None
    ) -> None:
        """Change a playlist's description and/or name (its songs stay)."""
        params = {"playlistId": playlist_id}
        if comment is not None:
            params["comment"] = comment
        if name is not None:
            params["name"] = name
        await self._call("updatePlaylist", params, post=True)

    async def delete_playlist(self, playlist_id: str) -> None:
        """Remove a playlist from Navidrome (only the snapshots Releasomat made itself)."""
        await self._call("deletePlaylist", {"id": playlist_id})
