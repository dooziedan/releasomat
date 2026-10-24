"""FastAPI application: startup (database, worker, scheduler), routes, static files.

Run it locally with ``uvicorn app.main:app --port 8430`` (see CLAUDE.md for the full command).
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app import __version__, events, files, pipeline, reports
from app import settings as user_settings
from app.config import get_config
from app.db import get_engine, migrate
from app.routes import activity, playlists, settings, system
from app.scheduler import PlaylistScheduler
from app.worker import Worker

log = logging.getLogger("app")


def configure_logging(level: str) -> None:
    """Log to the container's output at LOG_LEVEL (INFO if the value is unknown)."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        logging.getLogger().setLevel(level.upper())
    except ValueError:
        logging.getLogger().setLevel(logging.INFO)
        log.warning("Unknown LOG_LEVEL %r, using INFO", level)
    # httpx logs every request's full address at INFO, and Subsonic addresses carry Navidrome's
    # login token and salt (app/navidrome.py). Only its warnings and errors are logged.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    configure_logging(config.log_level)
    config.config_dir.mkdir(parents=True, exist_ok=True)
    migrate(config.database_url)
    engine = get_engine(config.database_url)
    pipeline.mark_interrupted(engine)
    files.clear_staging(config.config_dir)  # half-finished downloads of an interrupted run
    events.bus.reopen()

    with Session(engine) as session:
        download_limit = user_settings.get_int(session, "DOWNLOAD_CONCURRENCY")
    worker = Worker(engine, download_limit=download_limit)
    worker.start()
    scheduler = PlaylistScheduler(engine, worker.request_refresh, config.zone)
    scheduler.start()
    app.state.worker = worker
    app.state.scheduler = scheduler
    events.close_streams_on_exit()
    found = reports.versions()
    log.info(
        "Releasomat %s started (yt-dlp %s, Deno %s). Music folder: %s, time zone: %s",
        __version__,
        found["yt_dlp"],
        found["deno"],
        config.music_dir,
        config.zone.key,
    )
    try:
        yield
    finally:
        scheduler.shutdown()
        events.bus.close()
        await worker.stop()


app = FastAPI(
    title="Releasomat",
    version=__version__,
    summary="Turns playlists from streaming services into playlists in Navidrome.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(system.router)
app.include_router(playlists.router)
app.include_router(activity.router)
app.include_router(settings.router)
