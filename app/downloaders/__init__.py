"""Downloaders: one module per downloader, all implementing ``app/downloaders/base.py``.

Which ones are used, and in which order, is the global setting DOWNLOAD_SOURCES (Settings →
Download sources, ADR 0008); ``app/pipeline.py`` builds them from it.
"""

from app.downloaders.base import Downloader
from app.downloaders.slskd import SlskdDownloader
from app.downloaders.youtube import YouTubeDownloader

DOWNLOADERS: dict[str, type[Downloader]] = {d.name: d for d in (SlskdDownloader, YouTubeDownloader)}
