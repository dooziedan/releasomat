"""Deezer playlists, through Deezer's public API (no account needed).

``/playlist/<id>`` answers with the playlist and, for playlists up to 400 tracks, all its tracks
(each with an ISRC). Longer playlists are read from ``/playlist/<id>/tracks`` in pages of 500.
Deezer allows about 50 requests per 5 seconds; a short pause between pages stays far below that.
"""

import asyncio

import httpx

from app.httpclient import client
from app.sources.base import (
    PlaylistRef,
    PlaylistSnapshot,
    Source,
    SourceTrack,
    SourceType,
    UnsupportedLink,
    split_link,
)
from app.sources.web import get_json, require_tracks, source_error

OTHER_KINDS = {
    "album": "an album",
    "track": "a single track",
    "artist": "an artist",
    "show": "a podcast",
    "episode": "a podcast episode",
    "profile": "a profile",
}

API = "https://api.deezer.com"
PAGE_SIZE = 500
MAX_PAGES = 40  # 20,000 tracks; Deezer playlists hold at most 10,000
PAUSE = 0.2  # seconds between pages
NOT_FOUND = "Deezer can't find this playlist. It may have been deleted, or it is private."


class Deezer(Source):
    type = SourceType.DEEZER
    label = "Deezer"
    hosts = ("deezer.com", "deezer.page.link", "dzr.page.link")
    examples = ("www.deezer.com/…/playlist/…",)

    @classmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        link = split_link(url)
        if link.host != "deezer.com":  # link.deezer.com/s/…, deezer.page.link/…
            raise UnsupportedLink(
                "Short Deezer links can't be read yet. Open it in the browser and copy the full "
                "address (www.deezer.com/…/playlist/…)."
            )
        parts = link.parts
        if parts and len(parts[0]) == 2:  # the language: /de/playlist/…
            parts = parts[1:]
        if len(parts) >= 2 and parts[0] == "playlist" and parts[1].isdigit():
            return PlaylistRef(
                source=cls.type,
                id=parts[1],
                url=f"https://www.deezer.com/playlist/{parts[1]}",
                label="Deezer playlist",
            )
        if parts and parts[0] in OTHER_KINDS:
            raise UnsupportedLink(f"This is {OTHER_KINDS[parts[0]]} on Deezer, not a playlist.")
        raise UnsupportedLink("Paste the link of a Deezer playlist (www.deezer.com/playlist/…).")

    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        url = f"{API}/playlist/{ref.id}"
        async with client() as http:
            playlist = await _get(http, url)
            items = (playlist.get("tracks") or {}).get("data")
            total = playlist.get("nb_tracks")
            if not isinstance(items, list) or not isinstance(total, int) or len(items) < total:
                items = await _all_tracks(http, f"{url}/tracks")
        tracks = [track for item in items if (track := _track(item))]
        require_tracks(tracks, self.label, url, nb_tracks=playlist.get("nb_tracks"))
        warnings = []
        if len(tracks) < len(items):
            warnings.append(f"Skipped {len(items) - len(tracks)} entries without artist or title.")
        return PlaylistSnapshot(
            title=playlist.get("title") or "Deezer playlist",
            tracks=tracks,
            cover_url=playlist.get("picture_xl") or playlist.get("picture_big"),
            description=playlist.get("description") or None,
            warnings=warnings,
        )


async def _get(http: httpx.AsyncClient, url: str, **params) -> dict:
    """One API answer. Deezer reports errors as {"error": {…}} with HTTP status 200."""
    data = await get_json(http, url, service="Deezer", params=params, errors={404: NOT_FOUND})
    if not isinstance(data, dict):
        raise source_error(
            "Deezer sent an answer Releasomat doesn't understand.", url=url, excerpt=str(data)
        )
    error = data.get("error")
    if isinstance(error, dict):
        code, message = error.get("code"), str(error.get("message") or "")
        details = {"url": url, "deezer_error": f"{error.get('type')} {code}: {message}"}
        if code == 800:  # "no data"
            raise source_error(NOT_FOUND, **details)
        if code == 4:  # "Quota limit exceeded"
            raise source_error(
                "Deezer is limiting requests right now (too many requests). The next refresh "
                "tries again.",
                **details,
            )
        if code == 700:  # "Service busy"
            raise source_error("Deezer is busy right now. The next refresh tries again.", **details)
        raise source_error(f"Deezer refused the request: {message or code}.", **details)
    return data


async def _all_tracks(http: httpx.AsyncClient, url: str) -> list:
    """All tracks of a long playlist, page by page."""
    items: list = []
    for _ in range(MAX_PAGES):
        page = await _get(http, url, index=len(items), limit=PAGE_SIZE)
        data = page.get("data")
        if not isinstance(data, list):
            raise source_error(
                "Deezer sent a track list Releasomat doesn't understand.",
                url=url,
                found=sorted(page),
            )
        items.extend(data)
        if not data or not page.get("next"):
            break
        await asyncio.sleep(PAUSE)
    return items


def _track(item: object) -> SourceTrack | None:
    if not isinstance(item, dict):
        return None
    artist = (item.get("artist") or {}).get("name")
    title = item.get("title")
    if not artist or not title:
        return None
    album = item.get("album") or {}
    duration = item.get("duration")
    return SourceTrack(
        artist=artist,
        title=title,
        album=album.get("title") or None,
        duration=float(duration) if isinstance(duration, int | float) and duration > 0 else None,
        isrc=item.get("isrc") or None,
        cover_url=album.get("cover_xl") or album.get("cover_big") or None,
        source_id=str(item["id"]) if item.get("id") else None,
    )
