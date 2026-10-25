"""Database tables (docs/architecture.md, "Data model").

Choices like a run's status are stored as plain text. The ``StrEnum`` classes below list the
allowed values, so code can write ``RunStatus.FAILED`` instead of a bare ``"failed"``.
All times are in UTC, with the time zone attached (SQLModel refuses times without one and
gives them back in UTC); pages show them in the container's time zone (TZ).

After changing a table here, add a migration (see CLAUDE.md); ``test_migrations_match_models``
fails until you do.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Now, in UTC."""
    return datetime.now(UTC)


class PlaylistKind(StrEnum):
    PLAYLIST = "playlist"  # one playlist on the service
    SERIES = "series"  # ListenBrainz "Created for you": each refresh takes the newest one


class NavidromeMode(StrEnum):
    UPDATE = "update"  # keep one Navidrome playlist in sync
    SNAPSHOT = "snapshot"  # a new Navidrome playlist per refresh, keep the last N


class Trigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class RunStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # came due while the same playlist was still refreshing (ADR 0005)
    STOPPED = "stopped"  # the owner pressed Stop; the Navidrome playlist was left as it was


class FileStatus(StrEnum):
    """Where a track's song is now (the badge on the playlist page). What happened to it before
    is kept in ``TrackFile``'s time stamps (``adopted_at``, ``disliked_at``), so an adopted or
    disliked track can also be ``in_library`` once its song is found in the library."""

    PENDING = "pending"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    DONE = "done"
    IN_LIBRARY = "in_library"  # already in Navidrome: linked, never touched
    FAILED = "failed"
    SKIPPED = "skipped"
    # Tagwerk took the file (ADR 0009): never downloaded again; linked once it is in the library.
    ADOPTED = "adopted"
    # "Don't like": the owner deleted it (or it left Releasomat's folder and isn't anywhere else
    # in the library). Never downloaded again; linked if the song is elsewhere in the library.
    DISLIKED = "disliked"


class DislikeReason(StrEnum):
    """Why a track is marked "Don't like" (``TrackFile.disliked_reason``)."""

    VANISHED = "vanished"  # its file left Releasomat's folder and wasn't found anywhere else
    DELETED = "deleted"  # the owner deleted the file (in Releasomat, or with Tagwerk's Delete)
    DISCARDED = "discarded"  # the owner threw the adopted file away in Tagwerk's inbox
    OWNER = "owner"  # the owner pressed "Don't like"


class Playlist(SQLModel, table=True):
    """A playlist Releasomat follows, with its per-playlist settings."""

    __table_args__ = (UniqueConstraint("source", "source_id"),)

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)  # a SourceType: "apple_music", "listenbrainz", …
    kind: str = PlaylistKind.PLAYLIST.value
    # The service's ID of the playlist; for a series "<user>/<series>", e.g. "me/weekly-jams".
    source_id: str
    url: str
    title: str
    cover_url: str | None = None
    # Off: no scheduled refreshes (Refresh now still works).
    enabled: bool = True
    # A cron expression ("0 6 * * mon"); empty = only Refresh now. See app/scheduler.py.
    schedule: str = ""
    navidrome_mode: str = NavidromeMode.UPDATE.value
    snapshot_keep: int = 4  # snapshot mode: how many Navidrome playlists to keep
    # (Which download sources are tried, and in which order, is one global setting for all
    # playlists: DOWNLOAD_SOURCES, ADR 0008.)
    # Update mode: the Navidrome playlist Releasomat keeps in sync (its ID never changes).
    navidrome_playlist_id: str | None = None
    last_refresh_at: datetime | None = None
    next_refresh_at: datetime | None = None
    last_status: str | None = None  # the RunStatus of the latest run
    created_at: datetime = Field(default_factory=utcnow)


class Track(SQLModel, table=True):
    """One real-world track, shared by every playlist that contains it."""

    id: int | None = Field(default=None, primary_key=True)
    artist: str
    title: str
    album: str | None = None
    duration: float | None = None  # seconds
    isrc: str | None = Field(default=None, index=True)
    mbid: str | None = Field(default=None, index=True)  # MusicBrainz recording ID
    cover_url: str | None = None
    # "main artist|title", normalised (app/matching.py ``track_key``): finds the same track again
    # when a service gives neither an ISRC nor an MBID.
    match_key: str | None = Field(default=None, index=True)
    # From the playlist's source when it knows them (Beatport, Tidal), for Tagwerk (ADR 0009).
    bpm: float | None = None
    key: str | None = None  # musical key as the source writes it, e.g. "Gb Minor"
    created_at: datetime = Field(default_factory=utcnow)


class PlaylistTrack(SQLModel, table=True):
    """Which tracks a playlist has, in which order, and since when (or until when)."""

    id: int | None = Field(default=None, primary_key=True)
    playlist_id: int = Field(foreign_key="playlist.id", index=True)
    track_id: int = Field(foreign_key="track.id", index=True)
    position: int
    added_at: datetime = Field(default_factory=utcnow)
    removed_at: datetime | None = None  # set when the track left the playlist


class TrackFile(SQLModel, table=True):
    """Where a track lives: a song already in Navidrome, or a file Releasomat downloaded."""

    id: int | None = Field(default=None, primary_key=True)
    track_id: int = Field(foreign_key="track.id", index=True)
    navidrome_song_id: str | None = Field(default=None, index=True)
    # Relative to MUSIC_DIR. Empty when the song was already in the library (never touched).
    path: str | None = None
    # Where the file was in MUSIC_DIR before it left (adopted by Tagwerk, deleted by the owner,
    # moved away). Tagwerk's notices may still name a file by this path (app/lifecycle.py).
    former_path: str | None = Field(default=None, index=True)
    # Only files with this set may ever be deleted (ADR 0003).
    downloaded_by_releasomat: bool = False
    downloader: str | None = None  # "youtube", "slskd"
    format: str | None = None  # "mp3", "flac", "opus", …
    bitrate: int | None = None  # kbit/s
    status: str = Field(default=FileStatus.PENDING.value, index=True)
    error: str | None = None  # the latest problem, in plain words
    attempts: int = 0  # how many refreshes tried to download it
    # Details for the tracks table and the error report: how the song was found in Navidrome
    # ("matched_by": "mbid", "isrc", "name"), where a download came from ("origin"), and what was
    # tried and why it failed ("tried": [{"downloader", "candidate", "reason"}, …]).
    details: dict | None = Field(default=None, sa_column=Column(JSON))
    # What happened to the file (app/lifecycle.py, ADR 0009). These stay when the status
    # changes, e.g. an adopted track whose library copy is linked is "in_library" with
    # ``adopted_at`` still set.
    adopted_at: datetime | None = None  # Tagwerk adopted the file
    disliked_at: datetime | None = None  # marked "Don't like"
    disliked_reason: str | None = None  # a DislikeReason
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    """One refresh of one playlist, shown on the Activity page."""

    id: int | None = Field(default=None, primary_key=True)
    playlist_id: int = Field(foreign_key="playlist.id", index=True)
    trigger: str  # a Trigger
    status: str = Field(default=RunStatus.RUNNING.value, index=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    message: str | None = None  # a one-line summary, or the error
    tracks_total: int = 0
    tracks_in_library: int = 0
    tracks_downloaded: int = 0
    tracks_missing: int = 0
    tracks_failed: int = 0
    # One line per step, oldest first.
    log: str = ""
    # The error report when the run or single tracks failed (app/reports.py): what went wrong,
    # where, and the details needed to fix it. Never passwords, tokens or cookies.
    report: dict | None = Field(default=None, sa_column=Column(JSON))

    def add_log(self, line: str) -> None:
        self.log = f"{self.log}{line}\n"


class NavidromeSnapshot(SQLModel, table=True):
    """Snapshot mode: a Navidrome playlist Releasomat made for one refresh ("Mix 2026-10-22").
    Only playlists listed here are ever removed from Navidrome (when more than "keep" exist)."""

    id: int | None = Field(default=None, primary_key=True)
    playlist_id: int = Field(foreign_key="playlist.id", index=True)
    navidrome_id: str
    name: str
    created_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    """User settings saved in the UI (app/settings.py). Env vars of the same name win."""

    key: str = Field(primary_key=True)
    value: str
