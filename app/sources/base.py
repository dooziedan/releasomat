"""What every source (Apple Music, ListenBrainz, …) provides, and how a link finds its source.

A source does two things:

1. ``parse_url``: recognise a link to one of its playlists and pull out the playlist's ID.
   No network: this runs while the owner types on the Add playlist page.
2. ``fetch``: read the playlist from the service (name, cover, ordered tracks). Whatever goes
   wrong becomes a ``SourceError`` with a plain-language message and the details for the run's
   error report (docs/architecture.md, "Error reports"). Helpers for that are in ``web.py``.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

from app.models import utcnow


class SourceType(StrEnum):
    LISTENBRAINZ = "listenbrainz"
    APPLE_MUSIC = "apple_music"
    DEEZER = "deezer"
    TIDAL = "tidal"
    YOUTUBE = "youtube"
    YOUTUBE_MUSIC = "youtube_music"
    SOUNDCLOUD = "soundcloud"
    BEATPORT = "beatport"


class SourceError(Exception):
    """Reading a playlist from a service failed.

    The message is for the owner, in plain language ("Apple Music changed its playlist page:
    Releasomat can't find the track list"). ``details`` holds what's needed to fix it and goes
    into the run's error report (docs/architecture.md, "Error reports"): for example ``url``,
    ``http_status``, ``excerpt`` (a short piece of the answer), ``step``. Never passwords, tokens,
    API keys or cookies.
    """

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = details


class SourceNotReady(SourceError):
    """This source can recognise links, but can't read playlists yet.

    No source raises it any more; it stays for code that still catches it."""


class UnsupportedLink(ValueError):
    """The pasted text is not a playlist link Releasomat knows. The message says why."""


@dataclass(frozen=True)
class PlaylistRef:
    """Which playlist to follow: the result of recognising a link (no network involved)."""

    source: str  # a SourceType
    id: str  # the service's playlist ID; for a series "<user>/<series>"
    url: str  # the cleaned-up link (tracking parameters like ?si=… removed)
    kind: str = "playlist"  # "playlist" or "series" (models.PlaylistKind)
    label: str = ""  # e.g. "Apple Music playlist", shown before saving


@dataclass
class SourceTrack:
    """One track as the service lists it."""

    artist: str
    title: str
    album: str | None = None
    duration: float | None = None  # seconds
    isrc: str | None = None
    mbid: str | None = None  # MusicBrainz recording ID (ListenBrainz has them)
    cover_url: str | None = None
    source_id: str | None = None  # the track's ID on the service
    bpm: float | None = None  # Beatport and Tidal know it
    key: str | None = None  # musical key, e.g. "Gb Minor" (Beatport, Tidal)


@dataclass
class PlaylistSnapshot:
    """A playlist as it was at one refresh: name, cover and tracks in the service's order."""

    title: str
    tracks: list[SourceTrack]
    cover_url: str | None = None
    description: str | None = None
    # For a series: the ID of the concrete playlist that was the newest one this time.
    playlist_id: str | None = None
    fetched_at: datetime = field(default_factory=utcnow)
    # Things the owner should know that didn't stop the read, e.g. "Apple Music's page listed
    # 300 of the playlist's 512 tracks" or "Skipped 2 videos".
    warnings: list[str] = field(default_factory=list)


def user_setting(key: str) -> str:
    """A user setting (app/settings.py: env var, else saved value, else default)."""
    from sqlmodel import Session

    from app import settings
    from app.config import get_config
    from app.db import get_engine

    with Session(get_engine(get_config().database_url)) as session:
        return settings.get(session, key)


class Source(ABC):
    """Base class of all sources. Subclasses set the class attributes and implement both methods."""

    type: ClassVar[SourceType]
    label: ClassVar[str]  # "Apple Music"
    hosts: ClassVar[tuple[str, ...]]  # web addresses of the service, without "www."
    examples: ClassVar[tuple[str, ...]]  # what its links look like, for the Add playlist page

    def __init__(self, setting: Callable[[str], str] = user_setting) -> None:
        # How the source reads user settings such as LISTENBRAINZ_TOKEN. Tests pass their own.
        self.setting = setting

    @classmethod
    @abstractmethod
    def parse_url(cls, url: str) -> PlaylistRef:
        """The playlist a link points to. Raises UnsupportedLink for other links of the service
        (an album, a single track, …). Only called for links whose host is in ``hosts``."""

    @abstractmethod
    async def fetch(self, ref: PlaylistRef) -> PlaylistSnapshot:
        """Read the playlist from the service. Raises SourceError when that fails."""


# --- Helpers for parse_url ----------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """A pasted link, split up: host without "www."/"m.", path parts, query parameters."""

    host: str
    parts: list[str]
    query: dict[str, str]
    text: str  # the link as pasted (trimmed)


def split_link(text: str) -> Link:
    """Split a pasted link. Accepts links without "https://" ("music.apple.com/…")."""
    text = text.strip()
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    query = {key: values[0] for key, values in parse_qs(parts.query).items() if values}
    return Link(host, [p for p in parts.path.split("/") if p], query, text)


def detect_source(text: str) -> PlaylistRef:
    """The source and playlist of a pasted link. Raises UnsupportedLink with a helpful message."""
    from app.sources import SOURCES  # here, because the source modules import this module

    if not text or not text.strip():
        raise UnsupportedLink("Paste a link to a playlist.")
    stripped = text.strip()
    link = split_link(stripped)
    if not link.host or "." not in link.host:
        raise UnsupportedLink("That doesn't look like a link. Paste the playlist's address.")
    # Exact hosts first, so music.youtube.com goes to YouTube Music, not to YouTube.
    for source in SOURCES.values():
        if link.host in source.hosts:
            return source.parse_url(stripped)
    for source in SOURCES.values():  # subdomains: beta.listenbrainz.org, listen.tidal.com
        if any(link.host.endswith("." + h) for h in source.hosts):
            return source.parse_url(stripped)
    names = ", ".join(s.label for s in SOURCES.values())
    raise UnsupportedLink(f"Releasomat doesn't know {link.host}. Supported: {names}.")
