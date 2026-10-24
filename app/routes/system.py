"""Health check and basic status of the running instance."""

from fastapi import APIRouter

from app import __version__, reports
from app.config import ConfigDep

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict[str, str]:
    """Used by the Docker HEALTHCHECK (shown as healthy/unhealthy on Unraid)."""
    return {"status": "ok"}


@router.get("/api/status")
def status(config: ConfigDep) -> dict:
    """Versions (Releasomat, yt-dlp, Deno), mounted folders and time zone."""
    return {
        "version": __version__,
        "versions": reports.versions(),
        "youtube_cookies": (config.config_dir / "cookies.txt").is_file(),
        "config_dir": str(config.config_dir),
        "music_dir": str(config.music_dir),
        "music_dir_found": config.music_dir.is_dir(),
        "slskd_downloads_dir": str(config.slskd_downloads_dir),
        "slskd_downloads_dir_found": config.slskd_downloads_dir.is_dir(),
        "time_zone": config.zone.key,
    }
