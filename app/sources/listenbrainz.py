"""ListenBrainz: public user playlists, and the "Created for you" series.

"Created for you" playlists (Weekly Exploration, Weekly Jams, Daily Jams) are a *series*: every
week or day ListenBrainz makes a new one. Releasomat follows the series, and each refresh takes
the newest playlist of it. A series has no link of its own; the Add playlist page offers it as a
choice for the ListenBrainz user from Settings.

Playlists come as JSPF (JSON) with a MusicBrainz recording ID per track. The token from Settings
is optional: it is needed for private playlists and gets higher rate limits. ListenBrainz was
often slow or down in 2026, so requests are tried three times within about a minute.
"""

import re
from urllib.parse import quote

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
from app.sources.web import get_json, html_to_text, ms_to_seconds, require_tracks, source_error

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
RECORDING = re.compile(r"musicbrainz\.org/recording/([0-9a-f-]{36})", re.IGNORECASE)

# The series' names as ListenBrainz uses them in its API ("source_patch").
SERIES = {
    "weekly-exploration": "Weekly Exploration",
    "weekly-jams": "Weekly Jams",
    "daily-jams": "Daily Jams",
}

API = "https://api.listenbrainz.org/1"
RETRY_DELAYS = (5, 15)  # three tries within about a minute
PAGE_SIZE = 100  # "Created for you" playlists per request (ListenBrainz's default is 25)
MAX_PAGES = 20
COVER = "https://coverartarchive.org/release/{release}/{image}-500.jpg"
JSPF_PLAYLIST = "https://musicbrainz.org/doc/jspf#playlist"
JSPF_TRACK = "https://musicbrainz.org/doc/jspf#track"
UNAVAILABLE = (
    "ListenBrainz is down or very slow right now (no answer after 3 tries within a minute). "
    "The next refresh tries again."
)


class ListenBrainz(Source):
    type = SourceType.LISTENBRAINZ
    label = "ListenBrainz"
    hosts = ("listenbrainz.org",)
    examples = ("listenbrainz.org/playlist/…",)

    @classmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        link = split_link(url)
        parts = link.parts
        if len(parts) >= 2 and parts[0] == "playlist" and UUID.match(parts[1]):
            playlist_id = parts[1].lower()
            return PlaylistRef(
                source=cls.type,
                id=playlist_id,
                url=f"https://listenbrainz.org/playlist/{playlist_id}/",
                label="ListenBrainz playlist",
            )
        if parts[:1] == ["user"]:
            raise UnsupportedLink(
                "This is a ListenBrainz user page. Paste the link of one playlist "
                '(listenbrainz.org/playlist/…), or choose a "Created for you" series below.'
            )
        raise UnsupportedLink(
            "Paste the link of a ListenBrainz playlist (listenbrainz.org/playlist/…)."
        )

    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        token = self.setting("LISTENBRAINZ_TOKEN")
        headers = {"Authorization": f"Token {token}"} if token else {}
        async with client(headers=headers) as http:

            async def get(url: str, errors: dict[int, str], **params) -> object:
                return await get_json(
                    http,
                    url,
                    service=self.label,
                    params=params,
                    errors={401: BAD_TOKEN, **errors},
                    hide=(token,),
                    retry_delays=RETRY_DELAYS,
                    unavailable=UNAVAILABLE,
                )

            series_playlist = None
            if ref.kind == "series":
                series_playlist = await _newest_of_series(get, ref.id)
                playlist_id = series_playlist
            else:
                playlist_id = ref.id
            url = f"{API}/playlist/{playlist_id}"
            data = await get(url, {404: _not_found(token), 403: PRIVATE})
        return _snapshot(data, url, series_playlist)


BAD_TOKEN = "ListenBrainz didn't accept the user token. Check it in Settings → ListenBrainz."
PRIVATE = "This ListenBrainz playlist is private, and the token in Settings can't read it."


def _not_found(token: str) -> str:
    message = "ListenBrainz can't find this playlist. It may have been deleted, or it is private"
    if token:
        return message + "."
    return message + ": to read your own private playlists, add your user token in Settings."


async def _newest_of_series(get, ref_id: str) -> str:
    """The MBID of the newest playlist of a "Created for you" series ("<user>/<series>")."""
    user, _, series = ref_id.rpartition("/")
    url = f"{API}/user/{quote(user, safe='')}/playlists/createdfor"
    newest: dict | None = None
    offset = 0
    for _ in range(MAX_PAGES):
        page = await get(
            url,
            {404: f"ListenBrainz doesn't know the user “{user}”. Check the user name."},
            count=PAGE_SIZE,
            offset=offset,
        )
        playlists = page.get("playlists") if isinstance(page, dict) else None
        if not isinstance(playlists, list):
            raise _changed(url, page, "a list of playlists")
        for item in playlists:
            playlist = item.get("playlist") if isinstance(item, dict) else None
            if not isinstance(playlist, dict) or _series_of(playlist) != series:
                continue
            if newest is None or _date(playlist) > _date(newest):
                newest = playlist
        offset += len(playlists)
        if not playlists or offset >= int(page.get("playlist_count") or 0):
            break
    if newest is None:
        name = SERIES.get(series, series)
        hint = (
            " Daily Jams are only made for users who follow “troi-bot” on ListenBrainz."
            if series == "daily-jams"
            else " ListenBrainz makes them every Monday for users with enough listens."
        )
        raise source_error(
            f"ListenBrainz has no {name} playlist for {user} yet.{hint}",
            url=url,
            series=series,
            playlists_checked=offset,
        )
    playlist_id = _mbid(newest.get("identifier"), "playlist")
    if not playlist_id:
        raise _changed(url, newest, "the playlist's ID")
    return playlist_id


def _snapshot(data: object, url: str, series_playlist: str | None) -> PlaylistSnapshot:
    playlist = data.get("playlist") if isinstance(data, dict) else None
    items = playlist.get("track") if isinstance(playlist, dict) else None
    if not isinstance(playlist, dict) or not isinstance(items, list):
        raise _changed(url, data, "a playlist with tracks")
    tracks = [track for item in items if (track := _track(item))]
    skipped = len(items) - len(tracks)
    require_tracks(tracks, "ListenBrainz", url)
    return PlaylistSnapshot(
        title=playlist.get("title") or "ListenBrainz playlist",
        tracks=tracks,
        cover_url=next((t.cover_url for t in tracks if t.cover_url), None),
        description=html_to_text(playlist.get("annotation")),
        playlist_id=series_playlist,
        warnings=[f"Skipped {skipped} tracks without artist or title."] if skipped else [],
    )


def _track(item: object) -> SourceTrack | None:
    if not isinstance(item, dict) or not item.get("title") or not item.get("creator"):
        return None
    extension = (item.get("extension") or {}).get(JSPF_TRACK) or {}
    metadata = extension.get("additional_metadata") or {}
    cover = None
    if metadata.get("caa_release_mbid") and metadata.get("caa_id"):
        cover = COVER.format(release=metadata["caa_release_mbid"], image=metadata["caa_id"])
    mbid = _mbid(item.get("identifier"), "recording")
    return SourceTrack(
        artist=item["creator"],
        title=item["title"],
        album=item.get("album") or None,
        duration=ms_to_seconds(item.get("duration")),
        mbid=mbid,
        cover_url=cover,
        source_id=mbid,
    )


def _mbid(identifier: object, kind: str) -> str | None:
    """The MBID out of 'https://musicbrainz.org/recording/<mbid>' (or a list of such links),
    or out of 'https://listenbrainz.org/playlist/<mbid>' for kind "playlist"."""
    links = identifier if isinstance(identifier, list) else [identifier]
    pattern = RECORDING if kind == "recording" else re.compile(r"/playlist/([0-9a-f-]{36})")
    for link in links:
        if isinstance(link, str) and (found := pattern.search(link)):
            return found.group(1).lower()
    return None


def _series_of(playlist: dict) -> str | None:
    extension = (playlist.get("extension") or {}).get(JSPF_PLAYLIST) or {}
    metadata = extension.get("additional_metadata") or {}
    return (metadata.get("algorithm_metadata") or {}).get("source_patch")


def _date(playlist: dict) -> str:
    """The playlist's creation time. ListenBrainz sends ISO dates in UTC, which sort as text."""
    return str(playlist.get("date") or "")


def _changed(url: str, data: object, expected: str):
    found = sorted(data) if isinstance(data, dict) else type(data).__name__
    return source_error(
        f"ListenBrainz sent an answer Releasomat doesn't understand (expected {expected}).",
        url=url,
        found=found,
        excerpt=str(data),
    )


def series_ref(user: str, series: str) -> PlaylistRef:
    """A "Created for you" series of a ListenBrainz user, e.g. ("me", "weekly-jams")."""
    user = (user or "").strip()
    if series not in SERIES:
        raise UnsupportedLink(f"Unknown series {series!r}. Choose one of: {', '.join(SERIES)}.")
    if not user:
        raise UnsupportedLink("Enter your ListenBrainz user name (or set it in Settings).")
    if "/" in user or len(user) > 100 or not user.isprintable():
        raise UnsupportedLink("That is not a ListenBrainz user name.")
    return PlaylistRef(
        source=SourceType.LISTENBRAINZ,
        id=f"{user}/{series}",
        url=f"https://listenbrainz.org/user/{quote(user)}/",
        kind="series",
        label=f"ListenBrainz · {SERIES[series]} for {user}",
    )


def series_title(ref_id: str) -> str:
    """'me/weekly-jams' -> 'Weekly Jams (me)', the name until the first refresh brings one."""
    user, _, series = ref_id.rpartition("/")
    return f"{SERIES.get(series, series)} ({user})"
