"""SoundCloud sets (playlists): soundcloud.com/<user>/sets/<name>.

Read with yt-dlp, which finds SoundCloud's web client ID by itself. A flat list of a set only has
the tracks' links, so yt-dlp reads every track's details too (about a quarter of a second per
track), but without looking up the audio formats. The artist comes from the track's metadata,
else from an "Artist - Title" track name, else from the uploader (``titles.py``).
"""

import asyncio
import re

from app.sources import ytdlp
from app.sources.base import (
    PlaylistRef,
    PlaylistSnapshot,
    Source,
    SourceTrack,
    SourceType,
    UnsupportedLink,
    split_link,
)
from app.sources.titles import split_artist_title
from app.sources.web import require_tracks

NAME = re.compile(r"^[A-Za-z0-9_-]+$")
SECRET = re.compile(r"^s-[A-Za-z0-9]+$")  # private sets have a secret token after the name

# Track details only: "none" matches no audio format, so yt-dlp doesn't look any up.
OPTIONS = {
    "ignoreerrors": True,  # one track that can't be read doesn't stop the whole set
    "ignore_no_formats_error": True,
    "extractor_args": {"soundcloud": {"formats": ["none"]}},
}


class SoundCloud(Source):
    type = SourceType.SOUNDCLOUD
    label = "SoundCloud"
    hosts = ("soundcloud.com", "soundcloud.app.goo.gl")
    examples = ("soundcloud.com/…/sets/…",)

    @classmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        link = split_link(url)
        if link.host != "soundcloud.com":  # on.soundcloud.com/…, soundcloud.app.goo.gl/…
            raise UnsupportedLink(
                "Short SoundCloud links can't be read yet. Open it in the browser and copy the "
                "full address (soundcloud.com/…/sets/…)."
            )
        parts = link.parts
        if len(parts) >= 3 and parts[1] == "sets" and NAME.match(parts[0]) and NAME.match(parts[2]):
            set_id = "/".join(parts[:3])
            if len(parts) >= 4 and SECRET.match(parts[3]):
                set_id += "/" + parts[3]
            return PlaylistRef(
                source=cls.type,
                id=set_id,
                url=f"https://soundcloud.com/{set_id}",
                label="SoundCloud playlist",
            )
        if len(parts) == 1:
            raise UnsupportedLink("This is a SoundCloud profile, not a playlist.")
        if len(parts) == 2 and parts[1] not in ("sets", "likes", "tracks", "albums", "reposts"):
            raise UnsupportedLink("This is a single track on SoundCloud, not a playlist.")
        raise UnsupportedLink("Paste the link of a SoundCloud playlist (soundcloud.com/…/sets/…).")

    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        try:
            info, errors = await asyncio.to_thread(ytdlp.extract, ref.url, **OPTIONS)
        except Exception as exc:
            raise ytdlp.error(self.label, ref.url, str(exc), type(exc).__name__) from exc
        if not isinstance(info, dict):  # the set itself failed; yt-dlp said why
            message = errors[0] if errors else "yt-dlp returned nothing"
            raise ytdlp.error(self.label, ref.url, message, "DownloadError")
        entries = [e for e in info.get("entries") or [] if isinstance(e, dict)]
        tracks = [track for entry in entries if (track := _track(entry))]
        total = info.get("playlist_count")
        require_tracks(tracks, self.label, ref.url, playlist_count=total, errors=errors[:3])
        warnings = []
        unread = (total if isinstance(total, int) else len(entries)) - len(tracks)
        if unread > 0:
            warnings.append(f"Skipped {unread} tracks SoundCloud didn't give details for.")
        return PlaylistSnapshot(
            title=info.get("title") or "SoundCloud playlist",
            tracks=tracks,
            cover_url=_thumbnail(info),
            description=info.get("description") or None,
            warnings=warnings,
        )


def _track(entry: dict) -> SourceTrack | None:
    title = entry.get("title")
    if not title:
        return None
    # The label's metadata, if the uploader filled it in: ["Sara Landry, Alt8, Saint Monet"].
    named = [a for a in entry.get("artists") or [] if isinstance(a, str) and a.strip()]
    artist, song = split_artist_title(title, ", ".join(named) or entry.get("uploader"))
    if not artist:
        return None
    duration = entry.get("duration")
    is_album = entry.get("album_type") in ("album", "ep", "single", "compilation")
    return SourceTrack(
        artist=artist,
        title=song,
        album=entry.get("album") if is_album else None,  # a playlist's name isn't an album
        duration=float(duration) if isinstance(duration, int | float) and duration > 0 else None,
        cover_url=_thumbnail(entry),
        source_id=str(entry["id"]) if entry.get("id") else None,
    )


def _thumbnail(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    pictures = [p for p in info.get("thumbnails") or [] if isinstance(p, dict) and p.get("url")]
    return pictures[-1]["url"] if pictures else None
