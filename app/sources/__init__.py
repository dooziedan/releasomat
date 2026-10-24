"""Playlist sources: one module per service, all implementing ``app/sources/base.py``."""

from app.sources.apple_music import AppleMusic
from app.sources.base import Source, SourceType
from app.sources.beatport import Beatport
from app.sources.deezer import Deezer
from app.sources.listenbrainz import ListenBrainz
from app.sources.soundcloud import SoundCloud
from app.sources.tidal import Tidal
from app.sources.youtube import YouTube, YouTubeMusic

# In the order the Add playlist page lists them.
SOURCES: dict[str, type[Source]] = {
    source.type: source
    for source in (
        ListenBrainz,
        AppleMusic,
        Deezer,
        Tidal,
        YouTubeMusic,
        YouTube,
        SoundCloud,
        Beatport,
    )
}


def get_source(source_type: str) -> Source:
    """A ready-to-use source for a playlist's ``source`` column."""
    return SOURCES[SourceType(source_type)]()


def label(source_type: str) -> str:
    """'apple_music' -> 'Apple Music' (the stored value itself if unknown)."""
    source = SOURCES.get(source_type)
    return source.label if source else source_type
