"""Releasomat's download folder: the files it downloaded that are in MUSIC_DIR now (phase 4).

Everything here only looks; nothing changes a file or a row. The page "Downloads" and
``/api/downloads`` show this list, and the Tagwerk integration (``/api/integration/files``) uses
the same checks. Deleting one of these files is the owner's delete
(``lifecycle.delete_and_dislike``, ADR 0003).

A file is listed when Releasomat downloaded it, its row still has a path, and that path is a
plain file inside MUSIC_DIR. Files that left the folder (adopted by Tagwerk, deleted, moved) are
not listed; the next refresh notices those (``pipeline.files_that_left``).
"""

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from app import files
from app.models import Playlist, PlaylistTrack, Track, TrackFile


@dataclass
class PlaylistRef:
    id: int
    title: str


@dataclass
class FolderFile:
    """One downloaded file in Releasomat's folder, with the track it is for."""

    id: int  # the TrackFile's id
    track_id: int
    artist: str
    title: str
    album: str | None
    duration: float | None
    path: str  # relative to MUSIC_DIR
    size: int  # bytes
    downloaded_at: datetime
    downloader: str | None
    format: str | None
    bitrate: int | None
    # The playlists that contain the track now. Empty: no playlist uses the file any more.
    playlists: list[PlaylistRef] = field(default_factory=list)

    @property
    def unused(self) -> bool:
        return not self.playlists


@dataclass
class Summary:
    """How many files the folder has, and how much space they take (all, and the unused ones)."""

    count: int
    size: int
    unused_count: int
    unused_size: int


def stat(music_dir: Path, path: str) -> os.stat_result | None:
    """The file's size and time if it is a plain file inside Releasomat's folder, else None."""
    try:
        target = files.inside(music_dir, path)
        if target.is_symlink() or not target.is_file():
            return None
        return target.stat()
    except (files.UnsafePath, OSError):
        return None


def downloaded_at(file: TrackFile, mtime: float) -> datetime:
    """When the download was saved (kept since 2026-10; for older files the file's time)."""
    stored = (file.details or {}).get("downloaded_at")
    if stored:
        try:
            return datetime.fromisoformat(stored)
        except ValueError:
            pass
    return datetime.fromtimestamp(mtime, UTC)


def playlists_of(session: Session, track_ids: list[int]) -> dict[int, list[PlaylistRef]]:
    """The playlists that contain each track now, by title (one query). A track that is in a
    playlist twice is listed once."""
    if not track_ids:
        return {}
    rows = session.exec(
        select(PlaylistTrack.track_id, Playlist.id, Playlist.title)
        .join(Playlist, Playlist.id == PlaylistTrack.playlist_id)
        .where(PlaylistTrack.track_id.in_(track_ids), PlaylistTrack.removed_at.is_(None))
        .order_by(Playlist.title)
    ).all()
    found: dict[int, list[PlaylistRef]] = {}
    for track_id, playlist_id, title in rows:
        mine = found.setdefault(track_id, [])
        if all(p.id != playlist_id for p in mine):
            mine.append(PlaylistRef(playlist_id, title))
    return found


def folder_files(session: Session, music_dir: Path, file_id: int | None = None) -> list[FolderFile]:
    """The downloaded files in Releasomat's folder now, by artist and title. With ``file_id``
    only that one (an empty list when it isn't in the folder)."""
    query = (
        select(TrackFile, Track)
        .join(Track, Track.id == TrackFile.track_id)
        .where(TrackFile.downloaded_by_releasomat.is_(True), TrackFile.path.is_not(None))
    )
    if file_id is not None:
        query = query.where(TrackFile.id == file_id)
    rows = session.exec(query).all()
    lists = playlists_of(session, [track.id for _, track in rows])
    found = []
    for file, track in rows:
        on_disk = stat(music_dir, file.path)
        if on_disk is None:
            continue
        found.append(
            FolderFile(
                id=file.id,
                track_id=track.id,
                artist=track.artist,
                title=track.title,
                album=track.album,
                duration=track.duration,
                path=file.path,
                size=on_disk.st_size,
                downloaded_at=downloaded_at(file, on_disk.st_mtime),
                downloader=file.downloader,
                format=file.format,
                bitrate=file.bitrate,
                playlists=lists.get(track.id, []),
            )
        )
    found.sort(key=lambda f: (f.artist.casefold(), f.title.casefold(), f.path))
    return found


def folder_file(session: Session, music_dir: Path, file_id: int) -> FolderFile | None:
    """One downloaded file in Releasomat's folder, or None if it isn't there (now)."""
    found = folder_files(session, music_dir, file_id)
    return found[0] if found else None


def summary(found: list[FolderFile]) -> Summary:
    unused = [f for f in found if f.unused]
    return Summary(
        count=len(found),
        size=sum(f.size for f in found),
        unused_count=len(unused),
        unused_size=sum(f.size for f in unused),
    )
