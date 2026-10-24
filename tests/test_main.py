"""The app starts, every page renders, and the database matches the models."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlmodel import SQLModel

from app import settings as user_settings
from app.config import Config

ROOT = Path(__file__).resolve().parent.parent


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_status_reports_the_folders(client, config):
    data = client.get("/api/status").json()
    assert data["music_dir_found"] is True
    assert data["slskd_downloads_dir_found"] is False  # optional, not mounted in the tests
    assert data["time_zone"] == "UTC"


def test_every_page_renders(client):
    playlist = client.post(
        "/api/playlists",
        json={"url": "https://listenbrainz.org/playlist/1a2b3c4d-1234-4abc-8def-0123456789ab/"},
    ).json()
    pages = {
        "/": "Playlists",
        "/playlists/new": "Add playlist",
        f"/playlists/{playlist['id']}": "Settings",
        "/activity": "Activity",
        "/settings": "Navidrome",
    }
    for path, text in pages.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert text in response.text, path
        assert "https://github.com/dooziedan/releasomat" in response.text  # AGPL: source link
    assert client.get("/docs").status_code == 200


def test_the_menu_shows_where_you_are(client):
    assert '<a href="/" aria-current="page">Playlists</a>' in client.get("/").text
    assert '<a href="/activity" aria-current="page">' in client.get("/activity").text
    assert '<a href="/" aria-current="page">' not in client.get("/settings").text


def test_static_files_are_served(client):
    for name in ("htmx.min.js", "sse.js", "pico.min.css", "theme.css", "app.css", "icon.svg"):
        assert client.get(f"/static/{name}").status_code == 200, name


def test_the_api_is_documented_and_the_pages_are_not(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/health",
        "/api/playlists",
        "/api/playlists/{playlist_id}",
        "/api/playlists/{playlist_id}/refresh",
        "/api/playlists/{playlist_id}/stop",
        "/api/sources/detect",
        "/api/schedule/preview",
        "/api/runs",
        "/api/runs/{run_id}/stop",
        "/api/events",
        "/api/settings",
        "/api/navidrome/test",
    ):
        assert path in paths, path
    assert "/" not in paths and "/settings" not in paths and "/partials/runs" not in paths


def test_no_newer_css_for_colours():
    """color-mix() and relative colours didn't work in the owner's browser (CLAUDE.md)."""
    for name in ("theme.css", "app.css"):
        text = _css_without_comments(name)
        assert "color-mix(" not in text and " from var(" not in text, name


def test_colours_only_in_theme_css():
    """app.css uses the tokens; the colour values themselves live in theme.css."""
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", _css_without_comments("app.css"))


def _css_without_comments(name: str) -> str:
    text = (ROOT / "app" / "static" / name).read_text()
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def test_migrations_match_models(engine):
    """Fails if a model was changed without adding a migration."""
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), SQLModel.metadata)
    assert diff == []


def test_the_unraid_template_has_every_setting():
    """A new container or user setting needs its field in the Unraid template too."""
    template = ROOT / "unraid" / "releasomat.xml"
    root = ET.parse(template).getroot()
    targets = {c.get("Target") for c in root.iter("Config")}
    for name in Config.model_fields:
        if name.endswith("_dir"):
            path = {"config_dir": "/config", "music_dir": "/music"}.get(name, "/slskd")
            assert path in targets, name
        elif name == "tz":
            assert "TZ" not in targets  # Unraid passes its own time zone (ADR 0002)
        else:
            assert name.upper() in targets, name
    for definition in user_settings.DEFINITIONS:
        assert definition.key in targets, definition.key
        field = next(c for c in root.iter("Config") if c.get("Target") == definition.key)
        assert field.get("Mask") == ("true" if definition.secret else "false"), definition.key
    assert "8430" in root.findtext("WebUI")
