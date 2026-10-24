"""YouTube and YouTube Music playlists.

Both use the same playlist IDs (the ``list=`` parameter). YouTube Music playlists are read with
ytmusicapi (songs with artists, album and length), YouTube ones with yt-dlp (videos: the artist
comes from the video title or the channel, see ``titles.py``). Both run as Python libraries, in
a thread, because they block while they work.
"""

import asyncio
import re

import requests
import ytmusicapi
from ytmusicapi.exceptions import YTMusicServerError

from app.sources import ytdlp
from app.sources.base import (
    Link,
    PlaylistRef,
    PlaylistSnapshot,
    Source,
    SourceTrack,
    SourceType,
    UnsupportedLink,
    split_link,
)
from app.sources.titles import split_artist_title
from app.sources.web import require_tracks, source_error

LIST_ID = re.compile(r"^[A-Za-z0-9_-]{12,}$")
PRIVATE_LISTS = {"WL": "Watch later", "LL": "Liked videos", "LM": "Liked music"}
GONE = {"[Private video]", "[Deleted video]", "[Unavailable video]"}


def list_id(link: Link, service: str) -> str:
    """The playlist ID from a link's ``list=`` parameter (or YouTube Music's /browse/VL…)."""
    value = link.query.get("list", "")
    if not value and link.parts[:1] == ["browse"] and len(link.parts) > 1:
        browse = link.parts[1]
        if not browse.startswith("VL"):
            raise UnsupportedLink(f"This is an album or artist page on {service}, not a playlist.")
        value = browse.removeprefix("VL")
    if value in PRIVATE_LISTS:
        raise UnsupportedLink(f"“{PRIVATE_LISTS[value]}” is private and can't be followed.")
    if value.startswith("RD"):
        raise UnsupportedLink(
            f"This is a {service} mix: it is made on the fly for each listener and can't be "
            "followed. Save it as a playlist first."
        )
    if LIST_ID.match(value):
        return value
    if link.parts[:1] == ["watch"] or link.host == "youtu.be":
        raise UnsupportedLink(f"This is a single video on {service}, not a playlist.")
    if link.parts and (link.parts[0].startswith("@") or link.parts[0] in ("channel", "c")):
        raise UnsupportedLink(f"This is a channel on {service}, not a playlist.")
    raise UnsupportedLink(f"Paste the link of a {service} playlist (…/playlist?list=…).")


class YouTubeMusic(Source):
    type = SourceType.YOUTUBE_MUSIC
    label = "YouTube Music"
    hosts = ("music.youtube.com",)
    examples = ("music.youtube.com/playlist?list=…",)

    @classmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        playlist_id = list_id(split_link(url), cls.label)
        return PlaylistRef(
            source=cls.type,
            id=playlist_id,
            url=f"https://music.youtube.com/playlist?list={playlist_id}",
            label="YouTube Music playlist",
        )

    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        try:
            data = await asyncio.to_thread(load_ytmusic_playlist, ref.id)
        except Exception as exc:
            raise _ytmusic_error(exc, ref.url) from exc
        if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
            found = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise source_error(
                "YouTube Music answered in a way Releasomat doesn't understand. Updating "
                "ytmusicapi may help.",
                url=ref.url,
                found=found,
                ytmusicapi_version=ytmusicapi.__version__,
            )
        items = data["tracks"]
        tracks = [track for item in items if (track := _ytmusic_track(item))]
        require_tracks(tracks, self.label, ref.url, entries=len(items))
        warnings = []
        if len(tracks) < len(items):
            warnings.append(f"Skipped {len(items) - len(tracks)} entries without artist or title.")
        return PlaylistSnapshot(
            title=data.get("title") or "YouTube Music playlist",
            tracks=tracks,
            cover_url=_last_thumbnail(data.get("thumbnails")),
            description=data.get("description") or None,
            warnings=warnings,
        )


def load_ytmusic_playlist(playlist_id: str) -> dict:
    """The playlist as ytmusicapi returns it, with all its tracks. Blocks; tests replace it."""
    return ytmusicapi.YTMusic().get_playlist(playlist_id, limit=None)


def _ytmusic_track(item: object) -> SourceTrack | None:
    if not isinstance(item, dict) or not item.get("title"):
        return None
    artists = [
        a["name"] for a in item.get("artists") or [] if isinstance(a, dict) and a.get("name")
    ]
    if not artists:
        return None
    album = item.get("album")
    duration = item.get("duration_seconds")
    return SourceTrack(
        artist=", ".join(artists),
        title=item["title"],
        album=album.get("name") if isinstance(album, dict) else None,
        duration=float(duration) if isinstance(duration, int | float) and duration > 0 else None,
        cover_url=_last_thumbnail(item.get("thumbnails")),
        source_id=item.get("videoId") or None,
    )


def _ytmusic_error(exc: Exception, url: str):
    """A plain-language SourceError for whatever ytmusicapi raised."""
    details = {
        "url": url,
        "error_type": type(exc).__name__,
        "excerpt": str(exc),
        "ytmusicapi_version": ytmusicapi.__version__,
    }
    text = str(exc)
    if isinstance(exc, requests.Timeout):
        message = "YouTube Music didn't answer in time. The next refresh tries again."
    elif isinstance(exc, requests.RequestException):
        message = "Releasomat couldn't reach YouTube Music. Is the internet down?"
    elif isinstance(exc, YTMusicServerError):
        if "429" in text:
            message = (
                "YouTube Music is limiting requests right now (too many requests). The next "
                "refresh tries again."
            )
        else:
            message = "YouTube Music has a problem right now. The next refresh tries again."
    elif isinstance(exc, KeyError | IndexError | TypeError):
        # ytmusicapi can't find the parts of the answer it expects: usually because the
        # playlist doesn't exist or is private, sometimes because YouTube Music changed.
        message = (
            "YouTube Music can't find this playlist. It may have been deleted, or it is "
            "private. If it opens fine in the browser, updating ytmusicapi may help."
        )
    else:
        message = "ytmusicapi couldn't read the YouTube Music playlist. Updating it may help."
    return source_error(message, **details)


class YouTube(Source):
    type = SourceType.YOUTUBE
    label = "YouTube"
    hosts = ("youtube.com", "youtu.be", "youtube-nocookie.com")
    examples = ("www.youtube.com/playlist?list=…",)

    @classmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        playlist_id = list_id(split_link(url), cls.label)
        return PlaylistRef(
            source=cls.type,
            id=playlist_id,
            url=f"https://www.youtube.com/playlist?list={playlist_id}",
            label="YouTube playlist",
        )

    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        # A flat list is one quick request per 100 videos: titles, channels and lengths.
        try:
            info, _ = await asyncio.to_thread(ytdlp.extract, ref.url, extract_flat="in_playlist")
        except Exception as exc:
            raise ytdlp.error(self.label, ref.url, str(exc), type(exc).__name__) from exc
        entries = info.get("entries") if isinstance(info, dict) else None
        if not isinstance(entries, list):
            raise ytdlp.error(self.label, ref.url, "yt-dlp found no list of videos", "NoEntries")
        tracks = [track for entry in entries if (track := _video_track(entry))]
        require_tracks(tracks, self.label, ref.url, entries=len(entries))
        warnings = []
        if len(tracks) < len(entries):
            warnings.append(
                f"Skipped {len(entries) - len(tracks)} private, deleted or unnamed videos."
            )
        return PlaylistSnapshot(
            title=info.get("title") or "YouTube playlist",
            tracks=tracks,
            cover_url=_last_thumbnail(info.get("thumbnails")),
            description=info.get("description") or None,
            warnings=warnings,
        )


def _video_track(entry: object) -> SourceTrack | None:
    if not isinstance(entry, dict):
        return None
    title = entry.get("title")
    if not title or title in GONE:
        return None
    artist, song = split_artist_title(title, entry.get("channel") or entry.get("uploader"))
    if not artist:
        return None
    duration = entry.get("duration")
    return SourceTrack(
        artist=artist,
        title=song,
        duration=float(duration) if isinstance(duration, int | float) and duration > 0 else None,
        cover_url=_last_thumbnail(entry.get("thumbnails")),
        source_id=entry.get("id") or None,
    )


def _last_thumbnail(thumbnails: object) -> str | None:
    """The last (largest) picture of a thumbnail list."""
    if isinstance(thumbnails, list):
        for picture in reversed(thumbnails):
            if isinstance(picture, dict) and picture.get("url"):
                return picture["url"]
    return None
