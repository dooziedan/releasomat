"""Shared test setup. Tests never go online and never see the developer's own settings."""

import httpcore
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import settings as user_settings
from app.config import Config, get_config
from app.db import get_engine, migrate

CONTAINER_VARS = ("TZ", "LOG_LEVEL", "PUID", "PGID", "UMASK")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Env vars of the shell running the tests (e.g. from dev/.env) must not leak into them."""
    for definition in user_settings.DEFINITIONS:
        monkeypatch.delenv(definition.key, raising=False)
    for name in CONTAINER_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Any real HTTP request fails. Tests that need an answer use respx, which sits on top of
    this and answers from saved data (tests/fixtures/online/)."""

    message = "Tests never go online: answer this request with respx"

    # Same names and arguments as the methods they replace, so respx can wrap them.
    def handle_request(self, request):
        raise RuntimeError(message)

    async def handle_async_request(self, request):
        raise RuntimeError(message)

    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", handle_request)
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", handle_async_request)


@pytest.fixture
def config(tmp_path, monkeypatch) -> Config:
    """Container settings pointing at temporary folders (the slskd folder is not mounted)."""
    music = tmp_path / "music"
    music.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MUSIC_DIR", str(music))
    monkeypatch.setenv("SLSKD_DOWNLOADS_DIR", str(tmp_path / "slskd"))
    get_config.cache_clear()
    yield get_config()
    get_config.cache_clear()


@pytest.fixture
def engine(config):
    config.config_dir.mkdir(parents=True, exist_ok=True)
    migrate(config.database_url)
    engine = get_engine(config.database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(config):
    """The app as the browser sees it. "with" runs startup: migrations, worker, scheduler."""
    from app.main import app

    with TestClient(app) as client:
        yield client
