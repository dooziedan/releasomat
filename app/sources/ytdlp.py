"""Reading YouTube playlists and SoundCloud sets with yt-dlp, used as a Python library.

``extract`` blocks while yt-dlp works: sources run it in a thread (``asyncio.to_thread``).
Failures become a SourceError with yt-dlp's own message and version in the details: when a site
changes, updating yt-dlp is usually the fix.
"""

import re

import yt_dlp

from app.sources.base import SourceError
from app.sources.web import source_error


class _Log:
    """Collects yt-dlp's messages for the error report instead of printing them."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        self.errors.append(message)


def extract(url: str, **options) -> tuple[dict | None, list[str]]:
    """What yt-dlp knows about a playlist, without downloading anything, and the errors it
    reported. With ``ignoreerrors`` the answer is None when the playlist itself failed."""
    log = _Log()
    settings = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 15,
        "logger": log,
        **options,
    }
    with yt_dlp.YoutubeDL(settings) as ydl:
        info = ydl.extract_info(url, download=False)
        return (ydl.sanitize_info(info) if info else None), log.errors


def version() -> str:
    return yt_dlp.version.__version__


def error(service: str, url: str, message: str, error_type: str) -> SourceError:
    """A plain-language SourceError for a yt-dlp failure message."""
    text = message.lower()
    if "429" in text or "too many requests" in text:
        plain = (
            f"{service} is limiting requests right now (too many requests). The next refresh "
            "tries again."
        )
    elif re.search(r"http error 5\d\d", text):
        plain = f"{service} has a problem right now. The next refresh tries again."
    elif any(
        sign in text
        for sign in ("does not exist", "not found", "404", "private", "unavailable", "removed")
    ):
        plain = (
            f"{service} can't find this playlist. It may have been deleted, or it is private. "
            "If it opens fine in the browser, updating yt-dlp may help."
        )
    elif "sign in" in text or "not a bot" in text:
        plain = (
            f"{service} asks Releasomat to sign in because it looks like a bot. The next refresh "
            "tries again."
        )
    elif any(sign in text for sign in ("timed out", "unable to download", "connection", "urlopen")):
        plain = f"Releasomat couldn't reach {service}. Is the internet down?"
    else:
        plain = f"yt-dlp couldn't read the {service} playlist. Updating yt-dlp may help."
    return source_error(
        plain,
        url=url,
        error_type=error_type,
        excerpt=message.removeprefix("ERROR: "),
        ytdlp_version=version(),
    )
