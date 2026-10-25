"""Adding playlists and changing their settings, shared by the pages and the JSON API."""

from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from app.models import (
    FileStatus,
    NavidromeMode,
    NavidromeSnapshot,
    Playlist,
    PlaylistKind,
    PlaylistTrack,
    Run,
    TrackFile,
)
from app.scheduler import InvalidSchedule, make_trigger
from app.sources import SOURCES, label
from app.sources.base import PlaylistRef
from app.sources.listenbrainz import series_title


class InvalidSettings(ValueError):
    """Settings that can't be saved; ``errors`` maps each field to what is wrong with it."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


class AlreadyFollowed(Exception):
    def __init__(self, playlist: Playlist):
        super().__init__(f"Releasomat already follows this playlist: “{playlist.title}”.")
        self.playlist = playlist


def find(session: Session, ref: PlaylistRef) -> Playlist | None:
    return session.exec(
        select(Playlist).where(Playlist.source == ref.source, Playlist.source_id == ref.id)
    ).first()


def placeholder_title(ref: PlaylistRef) -> str:
    """A name until the first refresh brings the real one."""
    if ref.kind == PlaylistKind.SERIES:
        return series_title(ref.id)
    short = ref.id if len(ref.id) <= 16 else ref.id[:14] + "…"
    return f"{label(ref.source)} playlist {short}"


def add(session: Session, ref: PlaylistRef) -> Playlist:
    """Follow a playlist (no network: the title is a placeholder until the first refresh)."""
    if ref.source not in SOURCES:
        raise InvalidSettings({"url": f"Unknown source {ref.source!r}."})
    existing = find(session, ref)
    if existing is not None:
        raise AlreadyFollowed(existing)
    playlist = Playlist(
        source=ref.source,
        kind=ref.kind,
        source_id=ref.id,
        url=ref.url,
        title=placeholder_title(ref),
    )
    session.add(playlist)
    session.commit()
    session.refresh(playlist)
    return playlist


def update(session: Session, playlist: Playlist, changes: dict) -> Playlist:
    """Change a playlist's settings. Only the keys given are changed; raises InvalidSettings
    (and changes nothing) when a value doesn't fit."""
    errors: dict[str, str] = {}
    values: dict = {}

    if "enabled" in changes:
        values["enabled"] = bool(changes["enabled"])
    if "schedule" in changes:
        expression = " ".join(str(changes["schedule"] or "").split())
        try:
            if expression:
                make_trigger(expression, ZoneInfo("UTC"))  # only checks it
            values["schedule"] = expression
        except InvalidSchedule as exc:
            errors["schedule"] = str(exc)
    if "navidrome_mode" in changes:
        if changes["navidrome_mode"] in set(NavidromeMode):
            values["navidrome_mode"] = str(changes["navidrome_mode"])
        else:
            errors["navidrome_mode"] = "Choose update or snapshot."
    _number(changes, "snapshot_keep", 1, 52, values, errors)
    if errors:
        raise InvalidSettings(errors)
    for key, value in values.items():
        setattr(playlist, key, value)
    session.add(playlist)
    session.commit()
    session.refresh(playlist)
    return playlist


def track_counts(session: Session, playlist_id: int) -> dict[str, int]:
    """Tracks now in the playlist: in the library, downloaded, failed, adopted (Tagwerk has the
    file, waiting for the library copy), "Don't like", missing (the rest: not found, not
    downloaded yet, or skipped). Downloaded means the file is there, even if Navidrome hasn't
    listed it yet. Adopted and "Don't like" tracks are not missing: they are never downloaded."""
    rows = session.exec(
        select(PlaylistTrack.track_id, TrackFile.status)
        .join(TrackFile, TrackFile.track_id == PlaylistTrack.track_id, isouter=True)
        .where(PlaylistTrack.playlist_id == playlist_id, PlaylistTrack.removed_at.is_(None))
    ).all()
    status = {}
    for track_id, file_status in rows:
        status[track_id] = status.get(track_id) or file_status
    values = list(status.values())
    counts = {
        "total": len(values),
        "in_library": values.count(FileStatus.IN_LIBRARY),
        "downloaded": values.count(FileStatus.DONE),
        "failed": values.count(FileStatus.FAILED),
        "adopted": values.count(FileStatus.ADOPTED),
        "disliked": values.count(FileStatus.DISLIKED),
    }
    counts["missing"] = counts["total"] - sum(v for k, v in counts.items() if k != "total")
    return counts


def remove(session: Session, playlist: Playlist) -> None:
    """Forget a playlist: its settings, track list and runs. Files are never touched (ADR 0003),
    and its playlist in Navidrome stays; tracks other playlists share stay too."""
    for model in (Run, PlaylistTrack, NavidromeSnapshot):
        for row in session.exec(select(model).where(model.playlist_id == playlist.id)).all():
            session.delete(row)
    session.delete(playlist)
    session.commit()


def _number(changes: dict, key: str, low: int, high: int, values: dict, errors: dict) -> None:
    if key not in changes:
        return
    try:
        number = int(changes[key])
    except (TypeError, ValueError):
        errors[key] = "Enter a whole number."
        return
    if not low <= number <= high:
        errors[key] = f"Between {low} and {high}."
    else:
        values[key] = number
