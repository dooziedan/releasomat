"""Apple Music playlists: shared and editorial playlists from music.apple.com.

Apple's public playlist page has the whole playlist as JSON in
``<script id="serialized-server-data">``: a header section (name, artwork, description) and one or
more track-list sections (title, artist, album, length, Apple's song ID). No account and no ISRC.
When Apple changes the page, reading fails with a clear error report; the saved page in
``tests/fixtures/online/apple_music_*`` shows what it looked like when it last worked.
"""

import re

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
from app.sources.web import (
    html_to_text,
    ms_to_seconds,
    page_title,
    request,
    require_tracks,
    script_ids,
    script_json,
    source_error,
)

# "pl.u-…" are playlists shared by people, "pl.<32 hex>" are Apple's own.
PLAYLIST_ID = re.compile(r"^pl\.(u-)?[A-Za-z0-9]+$")
STOREFRONT = re.compile(r"^[a-z]{2}$")
OTHER_KINDS = {
    "album": "an album",
    "song": "a single song",
    "artist": "an artist",
    "music-video": "a music video",
    "station": "a radio station",
    "curator": "a curator page",
}

SCRIPT_ID = "serialized-server-data"
HEADER_KIND = "containerDetailHeaderLockup"
TRACK_KIND = "trackLockup"
COVER_SIZE = 1000  # pixels; Apple's artwork links are templates ("…/{w}x{h}bb.{f}")
CHANGED = "Apple Music changed its playlist page: Releasomat can't find the track list."
NOT_FOUND = (
    "Apple Music can't find this playlist. It may have been deleted, or it is no longer shared "
    "(… → Share → Copy Link shares it)."
)


class AppleMusic(Source):
    type = SourceType.APPLE_MUSIC
    label = "Apple Music"
    hosts = ("music.apple.com", "itunes.apple.com")
    examples = ("music.apple.com/…/playlist/…/pl.…",)

    @classmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        link = split_link(url)
        parts = link.parts
        if parts[:1] == ["library"]:
            raise UnsupportedLink(
                "This playlist is in your own library. Share it first (… → Share → Copy Link) "
                "and paste that link."
            )
        storefront = parts[0].lower() if parts and STOREFRONT.match(parts[0].lower()) else "us"
        rest = parts[1:] if parts and STOREFRONT.match(parts[0].lower()) else parts
        if rest[:1] == ["playlist"]:
            playlist_id = next((p for p in rest[1:] if PLAYLIST_ID.match(p)), None)
            if playlist_id:
                slug = rest[1] if len(rest) > 2 and rest[1] != playlist_id else None
                path = f"{slug}/{playlist_id}" if slug else playlist_id
                return PlaylistRef(
                    source=cls.type,
                    id=playlist_id,
                    url=f"https://music.apple.com/{storefront}/playlist/{path}",
                    label="Apple Music playlist",
                )
        if rest and rest[0] in OTHER_KINDS:
            raise UnsupportedLink(f"This is {OTHER_KINDS[rest[0]]} on Apple Music, not a playlist.")
        raise UnsupportedLink(
            "Paste the link of an Apple Music playlist (music.apple.com/…/playlist/…/pl.…)."
        )

    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        async with client() as http:
            response = await request(
                http, "GET", ref.url, service=self.label, errors={404: NOT_FOUND}
            )
        return parse_page(response.text, ref.url)


def parse_page(page: str, url: str) -> PlaylistSnapshot:
    """The playlist from the page's JSON (separate from fetch, so tests can feed saved pages)."""
    try:
        data = script_json(page, SCRIPT_ID)
    except ValueError as exc:
        raise source_error(
            CHANGED, url=url, error_type=type(exc).__name__, excerpt=str(exc)
        ) from exc
    if data is None:
        raise source_error(
            CHANGED,
            url=url,
            looked_for=f'<script id="{SCRIPT_ID}">',
            found_scripts=script_ids(page),
            page_title=page_title(page),
            excerpt=page,
        )

    sections = _sections(data)
    if not sections:
        found = sorted(data) if isinstance(data, dict) else type(data).__name__
        raise source_error(CHANGED, url=url, looked_for="data → data → sections", found=found)
    header = next((s["items"][0] for s in sections if s["kind"] == HEADER_KIND and s["items"]), {})
    track_sections = [s for s in sections if s["kind"] == TRACK_KIND]
    if not track_sections:
        raise source_error(
            CHANGED,
            url=url,
            looked_for=f'a section with itemKind "{TRACK_KIND}"',
            found_sections=[f"{s['id']} ({s['kind']})" for s in sections],
        )

    items = [item for section in track_sections for item in section["items"]]
    tracks = [track for item in items if (track := _track(item))]
    require_tracks(tracks, "Apple Music", url, sections=len(track_sections), items=len(items))

    warnings = []
    if len(tracks) < len(items):
        warnings.append(f"Skipped {len(items) - len(tracks)} entries without artist or title.")
    total = header.get("trackCount")
    if isinstance(total, int) and total > len(items):
        warnings.append(
            f"Apple Music's page lists {len(items)} of the playlist's {total} tracks; "
            "Releasomat can only read those."
        )
    description = (header.get("modalPresentationDescriptor") or {}).get("paragraphText")
    return PlaylistSnapshot(
        title=header.get("title") or "Apple Music playlist",
        tracks=tracks,
        cover_url=_artwork(header.get("artwork")),
        description=html_to_text(description if isinstance(description, str) else None),
        warnings=warnings,
    )


def _sections(data: object) -> list[dict]:
    """The page's sections as {"id", "kind", "items"}: data → data[…] → data → sections."""
    pages = data.get("data") if isinstance(data, dict) else None
    sections = []
    for page in pages if isinstance(pages, list) else []:
        content = page.get("data") if isinstance(page, dict) else None
        for section in (content or {}).get("sections") or []:
            if not isinstance(section, dict):
                continue
            items = section.get("items") if isinstance(section.get("items"), list) else []
            sections.append(
                {
                    "id": str(section.get("id", "")),
                    "kind": section.get("itemKind"),
                    "items": [item for item in items if isinstance(item, dict)],
                }
            )
    return sections


def _track(item: dict) -> SourceTrack | None:
    title, artist = item.get("title"), item.get("artistName")
    if not isinstance(title, str) or not isinstance(artist, str) or not title or not artist:
        return None
    album = None
    links = item.get("tertiaryLinks")
    if isinstance(links, list) and links and isinstance(links[0], dict):
        album = _album_name(links[0].get("title"))
    identifiers = (item.get("contentDescriptor") or {}).get("identifiers") or {}
    song_id = identifiers.get("storeAdamID")
    return SourceTrack(
        artist=artist,
        title=title,
        album=album,
        duration=ms_to_seconds(item.get("duration")),
        cover_url=_artwork(item.get("artwork")),
        source_id=str(song_id) if song_id else None,
    )


def _album_name(name: object) -> str | None:
    """'expectations - Single' -> 'expectations' (Apple adds " - Single" and " - EP")."""
    if not isinstance(name, str) or not name:
        return None
    return re.sub(r" - (Single|EP)$", "", name)


def _artwork(artwork: object) -> str | None:
    """The artwork link at COVER_SIZE, from Apple's template ("…/{w}x{h}bb.{f}")."""
    if not isinstance(artwork, dict):
        return None
    link = (artwork.get("dictionary") or {}).get("url")
    if not isinstance(link, str) or not link:
        return None
    size = str(COVER_SIZE)
    for placeholder, value in (("{w}", size), ("{h}", size), ("{c}", "bb"), ("{f}", "jpg")):
        link = link.replace(placeholder, value)
    return link
