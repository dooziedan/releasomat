"""Container settings, read from environment variables (ADR 0002).

These describe the container itself: where its folders are mounted, which user it runs as, its
time zone. They can only be changed by the container's environment (on Unraid: the template).
Settings the owner edits in the web UI (Navidrome address, API keys, …) live in the database
instead, see ``app/settings.py``.
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends
from pydantic_settings import BaseSettings

log = logging.getLogger(__name__)


class Config(BaseSettings):
    """Each field is read from the env var of the same name in capitals (``MUSIC_DIR``…)."""

    # Database, yt-dlp's cookies.txt, cache: everything that must survive an update.
    config_dir: Path = Path("/config")
    # Releasomat's own folder inside the Navidrome library. The only place it writes music.
    music_dir: Path = Path("/music")
    # Optional: slskd's download folder, so finished Soulseek downloads can be moved to music_dir.
    slskd_downloads_dir: Path = Path("/slskd")
    # File ownership on Unraid. Used by docker/entrypoint.sh; shown on the Settings page.
    puid: int = 99
    pgid: int = 100
    umask: str = "022"
    # Time zone for schedules ("daily at 06:00" means 06:00 in this zone).
    tz: str = "UTC"
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.config_dir / 'releasomat.db'}"

    @property
    def zone(self) -> ZoneInfo:
        """The time zone from TZ; UTC if TZ is not a known zone name."""
        try:
            return ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("Unknown time zone TZ=%r, using UTC", self.tz)
            return ZoneInfo("UTC")


@lru_cache
def get_config() -> Config:
    return Config()


ConfigDep = Annotated[Config, Depends(get_config)]
