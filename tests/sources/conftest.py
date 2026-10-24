"""Fixtures for the source tests: saved answers, sources with test settings, nothing online.

The saved answers in tests/fixtures/online/ are real answers of the services, trimmed to a few
tracks. Tidal's follow its official API description (tidal-api-oas.json), because there were no
Tidal credentials to capture real ones; its two error answers are real.
"""

import json
from pathlib import Path

import pytest

from app.sources import deezer, listenbrainz, tidal, youtube, ytdlp

FIXTURES = Path(__file__).parent.parent / "fixtures" / "online"


class Saved:
    """Reads saved answers: ``saved.text("apple_music_playlist.html")``, ``saved.json(…)``."""

    def text(self, name: str) -> str:
        return (FIXTURES / name).read_text()

    def json(self, name: str):
        return json.loads(self.text(name))


@pytest.fixture
def saved() -> Saved:
    return Saved()


@pytest.fixture
def make():
    """``make(Tidal, TIDAL_CLIENT_ID="…")``: a source that reads these user settings (and ""
    for every other one) instead of the database."""

    def make(source_class, **settings: str):
        return source_class(setting=lambda key: settings.get(key, ""))

    return make


@pytest.fixture
def report():
    """Everything in an error's details as one string, to check that no secret is in it."""
    return lambda error: json.dumps(error.details, default=str, ensure_ascii=False)


@pytest.fixture(autouse=True)
def no_libraries_online(monkeypatch):
    """ytmusicapi and yt-dlp don't use httpx, so the global offline guard doesn't stop them.
    Tests that need them replace these functions with saved answers."""

    def online(*args, **kwargs):
        raise RuntimeError("Tests never go online: replace this call with a saved answer")

    monkeypatch.setattr(youtube, "load_ytmusic_playlist", online)
    monkeypatch.setattr(ytdlp, "extract", online)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    """No waiting between pages and retries, and no Tidal login left over from another test."""
    monkeypatch.setattr(listenbrainz, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(deezer, "PAUSE", 0)
    monkeypatch.setattr(tidal, "PAUSE", 0)
    tidal._tokens.clear()
    yield
    tidal._tokens.clear()
