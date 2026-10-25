"""Jinja2 setup shared by all pages, with the small formatting helpers templates use."""

import hashlib
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import REPOSITORY, __version__
from app.config import get_config
from app.reports import as_text
from app.scheduler import describe
from app.sources import label

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

STATUS_LABELS = {
    "running": "Refreshing",
    "waiting": "Waiting",
    "done": "Done",
    "failed": "Failed",
    "skipped": "Skipped",
    "stopping": "Stopping",
    "stopped": "Stopped",
}
# A track's state on the playlist page (its track_file status).
TRACK_LABELS = {
    "in_library": "In library",
    "done": "Downloaded",
    "failed": "Failed",
    "skipped": "Missing",
    "pending": "Missing",
    "searching": "Searching…",
    "downloading": "Downloading…",
    "adopted": "Adopted by Tagwerk",
    "disliked": "Don't like",
}
# Why a track is "Don't like" (track_file.disliked_reason), for the tracks table.
DISLIKE_REASONS = {
    "deleted": "You deleted its file",
    "discarded": "You threw it away in Tagwerk",
    "vanished": "Its file left Releasomat's folder",
    "owner": "You marked it Don't like",
}
# A download source's name in the tracks table (track_file.downloader).
DOWNLOADER_LABELS = {"slskd": "Soulseek", "youtube": "YouTube"}
MATCHED_BY = {
    "mbid": "same MusicBrainz ID",
    "isrc": "same ISRC",
    "name": "same artist, title and length",
    "path": "by its file",
}


def localtime(value: datetime | None, fmt: str = "%a %d %b %Y, %H:%M") -> str:
    """A stored time (UTC) in the container's time zone (TZ): "Tue 21 Oct 2026, 20:30"."""
    if value is None:
        return ""
    if value.tzinfo is None:  # just in case: stored times are UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(get_config().zone).strftime(fmt)


def duration(start: datetime | None, end: datetime | None) -> str:
    """How long a run took: "4 s", "2 min 5 s"."""
    if start is None or end is None:
        return ""
    seconds = max(0, int((end - start).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def clock(seconds: float | None) -> str:
    """A track's length: 225 -> "3:45"."""
    if not seconds:
        return ""
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d}"


@cache
def static_version(name: str) -> str:
    """A short fingerprint of a file in app/static, added to its URL (``theme.css?v=…``), so
    browsers load the new file after an update instead of an old cached copy."""
    path = Path(__file__).parent / "static" / name
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


templates.env.globals.update(
    version=__version__,
    repository=REPOSITORY,
    static_version=static_version,
    source_label=label,
    status_labels=STATUS_LABELS,
    describe_schedule=describe,
    track_labels=TRACK_LABELS,
    dislike_reasons=DISLIKE_REASONS,
    downloader_labels=DOWNLOADER_LABELS,
    matched_by=MATCHED_BY,
    report_text=as_text,
)
templates.env.filters.update(localtime=localtime, duration=duration, clock=clock)
