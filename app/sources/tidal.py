"""Tidal playlists, through Tidal's official API (v2, JSON:API format).

Tidal needs a free developer app (developer.tidal.com): its client ID and secret go into
Settings → Tidal. Releasomat logs in as that app ("client credentials", no Tidal user involved)
and reads public and unlisted playlists:

1. ``POST auth.tidal.com/v1/oauth2/token`` gives a token (kept until it expires);
2. ``GET /playlists/<id>`` gives the name, description and cover;
3. ``GET /playlists/<id>/relationships/items`` gives the track IDs in order, page by page;
4. ``GET /tracks?filter[id]=…&include=artists,albums`` gives title, ISRC, length, BPM, key,
   artists and album, 20 tracks at a time.

The token and the secret never appear in error reports.
"""

import asyncio
import re
import time

import httpx

from app.httpclient import client
from app.sources.base import (
    PlaylistRef,
    PlaylistSnapshot,
    Source,
    SourceError,
    SourceTrack,
    SourceType,
    UnsupportedLink,
    split_link,
)
from app.sources.web import iso_seconds, parse_json, request, require_tracks, source_error

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
OTHER_KINDS = {
    "album": "an album",
    "track": "a single track",
    "artist": "an artist",
    "video": "a video",
    "mix": "a Tidal mix: it is made for one listener and can't be followed",
    "user": "a profile",
}

API = "https://openapi.tidal.com/v2"
TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"
COUNTRY = "US"  # Tidal answers per country; the US catalogue is the largest
BATCH = 20  # tracks per /tracks request (the API's maximum)
MAX_PAGES = 500
PAUSE = 0.1  # seconds between requests; Tidal publishes no limits, so stay gentle
COVER_WIDTH = 1280
KEYS = {"CSharp": "C#", "FSharp": "F#"}

NO_CREDENTIALS = "Tidal needs a client ID and secret: Settings → Tidal."
BAD_CREDENTIALS = "Tidal didn't accept the client ID and secret. Check them in Settings → Tidal."
NOT_FOUND = (
    "Tidal can't find this playlist. It may have been deleted, or it is private (only public "
    "and unlisted playlists can be read)."
)
LOGIN_EXPIRED = "Tidal didn't accept Releasomat's login any more. The next refresh logs in again."

# Tokens by client ID: (token, when it expires on time.monotonic()'s clock).
_tokens: dict[str, tuple[str, float]] = {}


class Tidal(Source):
    type = SourceType.TIDAL
    label = "Tidal"
    hosts = ("tidal.com",)  # and its subdomains: listen.tidal.com, link.tidal.com
    examples = ("tidal.com/playlist/…", "listen.tidal.com/playlist/…")

    @classmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        link = split_link(url)
        if link.host == "link.tidal.com":
            raise UnsupportedLink(
                "Short Tidal links can't be read. Open it in the browser and copy the full "
                "address (tidal.com/playlist/…)."
            )
        parts = link.parts
        if parts[:1] == ["browse"]:  # tidal.com/browse/playlist/…
            parts = parts[1:]
        if len(parts) >= 2 and parts[0] == "playlist" and UUID.match(parts[1]):
            playlist_id = parts[1].lower()
            return PlaylistRef(
                source=cls.type,
                id=playlist_id,
                url=f"https://tidal.com/playlist/{playlist_id}",
                label="Tidal playlist",
            )
        if parts and parts[0] in OTHER_KINDS:
            raise UnsupportedLink(f"This is {OTHER_KINDS[parts[0]]} on Tidal, not a playlist.")
        raise UnsupportedLink("Paste the link of a Tidal playlist (tidal.com/playlist/…).")

    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        client_id = self.setting("TIDAL_CLIENT_ID").strip()
        secret = self.setting("TIDAL_CLIENT_SECRET").strip()
        if not client_id or not secret:
            raise source_error(NO_CREDENTIALS, url=ref.url)
        async with client(headers={"Accept": "application/vnd.api+json"}) as http:
            token = await _token(http, client_id, secret)
            api = _Api(http, token, hide=(client_id, secret, token))
            try:
                playlist = await api.get(f"/playlists/{ref.id}", include="coverArt")
                items = await api.items(ref.id)
                tracks, missing = await api.tracks(
                    [i["id"] for i in items if i["type"] == "tracks"]
                )
            except _LoginExpired:
                _tokens.pop(client_id, None)
                raise source_error(LOGIN_EXPIRED, url=ref.url, http_status=401) from None

        data = playlist.get("data") if isinstance(playlist.get("data"), dict) else {}
        attributes = data.get("attributes") or {}
        require_tracks(tracks, self.label, ref.url, items=len(items))
        warnings = []
        videos = sum(1 for item in items if item["type"] != "tracks")
        if videos:
            warnings.append(f"Skipped {videos} videos: Releasomat copies music only.")
        if missing:
            warnings.append(f"{missing} tracks aren't in Tidal's catalogue for {COUNTRY}.")
        return PlaylistSnapshot(
            title=attributes.get("name") or "Tidal playlist",
            tracks=tracks,
            cover_url=_cover(playlist),
            description=attributes.get("description") or None,
            warnings=warnings,
        )


class _LoginExpired(Exception):
    """The API answered 401 to a token that should still be valid."""


class _Api:
    """GET requests to Tidal's API with the token, turning failures into SourceErrors."""

    def __init__(self, http: httpx.AsyncClient, token: str, hide: tuple[str, ...]) -> None:
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}"}
        self.hide = hide

    async def get(self, path: str, **params) -> dict:
        try:
            response = await request(
                self.http,
                "GET",
                API + path,
                service="Tidal",
                params={"countryCode": COUNTRY, **params},
                headers=self.headers,
                hide=self.hide,
                errors={404: NOT_FOUND, 403: NOT_FOUND, 401: LOGIN_EXPIRED},
            )
        except SourceError as exc:
            if exc.details.get("http_status") == 401:
                raise _LoginExpired from exc
            raise
        data = parse_json(response, "Tidal", self.hide)
        if not isinstance(data, dict):
            raise source_error(
                "Tidal sent an answer Releasomat doesn't understand.",
                url=API + path,
                excerpt=str(data),
                hide=self.hide,
            )
        return data

    async def items(self, playlist_id: str) -> list[dict]:
        """The playlist's items in order: [{"id": "123", "type": "tracks"}, …]."""
        path = f"/playlists/{playlist_id}/relationships/items"
        items: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            page = await self.get(path, **({"page[cursor]": cursor} if cursor else {}))
            data = page.get("data")
            if not isinstance(data, list):
                raise source_error(
                    "Tidal sent a track list Releasomat doesn't understand.",
                    url=API + path,
                    found=sorted(page),
                )
            items += [
                {"id": str(item["id"]), "type": item.get("type")}
                for item in data
                if isinstance(item, dict) and item.get("id")
            ]
            cursor = ((page.get("links") or {}).get("meta") or {}).get("nextCursor")
            if not cursor or not data:
                break
            await asyncio.sleep(PAUSE)
        return items

    async def tracks(self, track_ids: list[str]) -> tuple[list[SourceTrack], int]:
        """The tracks with their artists and albums, in the given order, and how many of them
        Tidal didn't return (not available in COUNTRY)."""
        found: dict[str, SourceTrack] = {}
        for start in range(0, len(track_ids), BATCH):
            if start:
                await asyncio.sleep(PAUSE)
            batch = track_ids[start : start + BATCH]
            page = await self.get(
                "/tracks", **{"filter[id]": batch, "include": ["artists", "albums"]}
            )
            included = {
                (resource.get("type"), str(resource.get("id"))): resource
                for resource in page.get("included") or []
                if isinstance(resource, dict)
            }
            for resource in page.get("data") or []:
                if isinstance(resource, dict) and (track := _track(resource, included)):
                    found[track.source_id or ""] = track
        tracks = [found[track_id] for track_id in track_ids if track_id in found]
        return tracks, len(track_ids) - len(tracks)


async def _token(http: httpx.AsyncClient, client_id: str, secret: str) -> str:
    """A token for the client credentials, from the cache while it is valid."""
    cached = _tokens.get(client_id)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    hide = (client_id, secret)
    response = await request(
        http,
        "POST",
        TOKEN_URL,
        service="Tidal",
        data={"grant_type": "client_credentials"},
        auth=(client_id, secret),
        hide=hide,
        errors={400: BAD_CREDENTIALS, 401: BAD_CREDENTIALS, 403: BAD_CREDENTIALS},
    )
    data = parse_json(response, "Tidal", hide)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        found = sorted(data) if isinstance(data, dict) else type(data).__name__
        raise source_error("Tidal's login answer had no token.", url=TOKEN_URL, found=found)
    expires_in = data.get("expires_in")
    lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
    _tokens[client_id] = (token, time.monotonic() + max(0.0, lifetime - 60))
    return token


def _track(resource: dict, included: dict) -> SourceTrack | None:
    attributes = resource.get("attributes") or {}
    title = attributes.get("title")
    relationships = resource.get("relationships") or {}
    artists = [
        name
        for ref in (relationships.get("artists") or {}).get("data") or []
        if (name := _attribute(included, "artists", ref, "name"))
    ]
    if not title or not artists:
        return None
    version = attributes.get("version")
    if version and version.lower() not in title.lower():
        title = f"{title} ({version})"
    albums = (relationships.get("albums") or {}).get("data") or []
    key, scale = attributes.get("key"), attributes.get("keyScale")
    bpm = attributes.get("bpm")
    return SourceTrack(
        artist=", ".join(artists),
        title=title,
        album=_attribute(included, "albums", albums[0], "title") if albums else None,
        duration=iso_seconds(attributes.get("duration")),
        isrc=attributes.get("isrc") or None,
        source_id=str(resource.get("id")),
        bpm=float(bpm) if isinstance(bpm, int | float) and bpm > 0 else None,
        key=(
            f"{KEYS.get(key, key)} {scale.replace('_', ' ').title()}"
            if key and key != "UNKNOWN" and scale and scale != "UNKNOWN"
            else None
        ),
    )


def _attribute(included: dict, kind: str, ref: object, name: str) -> str | None:
    if not isinstance(ref, dict):
        return None
    resource = included.get((kind, str(ref.get("id"))))
    return ((resource or {}).get("attributes") or {}).get(name) or None


def _cover(playlist: dict) -> str | None:
    """The playlist's cover: the largest file up to COVER_WIDTH pixels wide."""
    files = [
        file
        for resource in playlist.get("included") or []
        if isinstance(resource, dict) and resource.get("type") == "artworks"
        for file in (resource.get("attributes") or {}).get("files") or []
        if isinstance(file, dict) and file.get("href")
    ]
    if not files:
        return None

    def width(file: dict) -> int:
        return int((file.get("meta") or {}).get("width") or 0)

    fitting = [f for f in files if width(f) <= COVER_WIDTH] or files
    return max(fitting, key=width)["href"]
