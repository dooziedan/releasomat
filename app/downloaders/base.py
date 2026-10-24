"""What every downloader (YouTube via yt-dlp, Soulseek via slskd) provides.

A downloader gets one track that is missing from Navidrome, finds the best copy it can, checks
it and downloads it into a staging folder it is given. Tagging the file and moving it into
MUSIC_DIR is the pipeline's job (``app/tagging.py``, ``app/files.py``), never the downloader's
(ADR 0003).

When a download fails, the downloader says why in plain words and lists what it tried
(``tried``: one entry per candidate with the reason it was not used). Both go into the run's
error report.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from app.sources.base import SourceTrack


class DownloaderError(Exception):
    """A download failed (nothing found, service unreachable, wrong file, …)."""

    def __init__(self, message: str, tried: list[dict] | None = None, **details: object):
        super().__init__(message)
        self.tried = tried or []
        self.details = details


class DownloaderNotReady(DownloaderError):
    """This downloader isn't set up or arrives in a later phase: the track is skipped."""


@dataclass
class DownloadResult:
    """A finished, checked download, still in the staging folder."""

    path: Path
    format: str  # "opus", "m4a", "mp3", "flac", …
    bitrate: int | None  # kbit/s
    downloader: str  # Downloader.name
    origin: str | None = None  # where it came from: a video URL, a Soulseek user and file
    duration: float | None = None  # seconds, as measured in the file
    cover_url: str | None = None  # a picture of the release (thumbnail), if the service has one
    album: str | None = None  # the album the service names, if any
    tried: list[dict] = field(default_factory=list)  # candidates that were rejected first


class Downloader(ABC):
    name: ClassVar[str]  # stored in playlist.downloaders and track_file.downloader
    label: ClassVar[str]

    @abstractmethod
    async def download(self, track: SourceTrack, staging_dir: Path) -> DownloadResult:
        """Find the track and download it into ``staging_dir``. Raises DownloaderError."""
