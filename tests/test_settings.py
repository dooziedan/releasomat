"""User settings: env var > saved in the database > default; secrets never shown in full."""

import re

import pytest

from app import settings as user_settings
from app.settings import InvalidSetting, SettingLocked, get, get_int, is_locked, mask, save


def test_default_then_saved_then_env(session, monkeypatch):
    assert get(session, "NAVIDROME_SUBFOLDER") == "Releasomat"  # the default
    assert user_settings.source_of(session, "NAVIDROME_SUBFOLDER") == "default"

    save(session, "NAVIDROME_SUBFOLDER", "Downloads")
    assert get(session, "NAVIDROME_SUBFOLDER") == "Downloads"
    assert user_settings.source_of(session, "NAVIDROME_SUBFOLDER") == "saved"

    monkeypatch.setenv("NAVIDROME_SUBFOLDER", "FromUnraid")
    assert get(session, "NAVIDROME_SUBFOLDER") == "FromUnraid"  # the env var wins
    assert is_locked("NAVIDROME_SUBFOLDER")
    assert user_settings.source_of(session, "NAVIDROME_SUBFOLDER") == "container"
    with pytest.raises(SettingLocked):
        save(session, "NAVIDROME_SUBFOLDER", "Other")

    monkeypatch.delenv("NAVIDROME_SUBFOLDER")
    assert get(session, "NAVIDROME_SUBFOLDER") == "Downloads"  # the saved value is still there
    save(session, "NAVIDROME_SUBFOLDER", "")  # empty: back to the default
    assert get(session, "NAVIDROME_SUBFOLDER") == "Releasomat"


def test_an_empty_env_var_does_not_lock(session, monkeypatch):
    """Unraid passes optional template fields left blank as empty variables."""
    monkeypatch.setenv("NAVIDROME_URL", "")
    assert not is_locked("NAVIDROME_URL")
    save(session, "NAVIDROME_URL", "http://192.168.1.10:4533/")
    assert get(session, "NAVIDROME_URL") == "http://192.168.1.10:4533"  # trailing / removed


def test_values_are_checked(session):
    with pytest.raises(InvalidSetting, match="http"):
        save(session, "NAVIDROME_URL", "192.168.1.10:4533")
    with pytest.raises(InvalidSetting, match="whole number"):
        save(session, "DOWNLOAD_CONCURRENCY", "two")
    with pytest.raises(InvalidSetting, match="Between 1 and 8"):
        save(session, "DOWNLOAD_CONCURRENCY", "50")
    assert get_int(session, "DOWNLOAD_CONCURRENCY") == 2
    save(session, "DOWNLOAD_CONCURRENCY", " 3 ")
    assert get_int(session, "DOWNLOAD_CONCURRENCY") == 3


def test_a_bad_env_value_falls_back_to_the_default(session, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_CONCURRENCY", "lots")
    assert get_int(session, "DOWNLOAD_CONCURRENCY") == 2


def test_secrets_are_masked():
    assert mask("") == ""
    assert mask("short") == "••••••••"
    assert mask("a-long-api-key-1234") == "••••••••34"
    assert "a-long-api-key" not in mask("a-long-api-key-1234")


def test_views_never_contain_a_secret(session, monkeypatch):
    save(session, "NAVIDROME_PASSWORD", "hunter2-very-secret")
    monkeypatch.setenv("SLSKD_API_KEY", "slskd-key-from-env-0042")
    views = {v.key: v for v in user_settings.all_views(session)}
    assert views["NAVIDROME_PASSWORD"].value == "••••••••et"
    assert views["NAVIDROME_PASSWORD"].is_set and not views["NAVIDROME_PASSWORD"].locked
    assert views["SLSKD_API_KEY"].value == "••••••••42" and views["SLSKD_API_KEY"].locked
    assert views["TIDAL_CLIENT_SECRET"].is_set is False


def test_api_masks_secrets_and_refuses_locked_settings(client, monkeypatch):
    response = client.put(
        "/api/settings",
        json={"NAVIDROME_URL": "http://navidrome:4533", "NAVIDROME_PASSWORD": "pw-123456789"},
    )
    assert response.status_code == 200
    text = client.get("/api/settings").text
    assert "pw-123456789" not in text and "http://navidrome:4533" in text

    assert client.put("/api/settings", json={"NOPE": "x"}).status_code == 422
    assert client.put("/api/settings", json={"NAVIDROME_URL": "ftp://x"}).status_code == 422
    monkeypatch.setenv("NAVIDROME_USER", "admin")
    assert client.put("/api/settings", json={"NAVIDROME_USER": "other"}).status_code == 409


def test_settings_page_keeps_secrets_and_shows_locked_fields(client, monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_USER", "dooziedan")
    form = {
        "NAVIDROME_URL": "http://192.168.1.10:4533",
        "NAVIDROME_USER": "admin",
        "NAVIDROME_PASSWORD": "first-password-1",
        "LISTENBRAINZ_USER": "ignored: locked",
    }
    response = client.post("/settings", data=form, follow_redirects=False)
    assert response.status_code == 303

    # The password field left empty keeps the saved password.
    client.post("/settings", data={**form, "NAVIDROME_PASSWORD": ""})
    page = client.get("/settings").text
    assert "first-password-1" not in page
    assert "Saved: ••••••••-1" in page
    assert "set by the container" in page
    assert 'value="dooziedan"' in page and "disabled" in page
    views = {v["key"]: v for v in client.get("/api/settings").json()}
    assert views["NAVIDROME_PASSWORD"]["is_set"] is True
    assert views["LISTENBRAINZ_USER"]["source"] == "container"

    # Its "Remove" box clears it.
    client.post(
        "/settings", data={**form, "NAVIDROME_PASSWORD": "", "clear_NAVIDROME_PASSWORD": "on"}
    )
    views = {v["key"]: v for v in client.get("/api/settings").json()}
    assert views["NAVIDROME_PASSWORD"]["is_set"] is False


def test_settings_form_shows_errors(client):
    response = client.post("/settings", data={"NAVIDROME_URL": "navidrome:4533"})
    assert response.status_code == 422
    assert "Start with http:// or https://" in response.text
    assert 'value="navidrome:4533"' in response.text  # what was typed stays


def test_switches(session, monkeypatch):
    """A "bool" setting: a switch on the Settings page, "true"/"false" from an env var."""
    assert get(session, "SNAPSHOT_AUTO_DELETE") == "false"  # off unless the owner turns it on
    assert user_settings.get_bool(session, "SNAPSHOT_AUTO_DELETE") is False
    save(session, "SNAPSHOT_AUTO_DELETE", "on")
    assert get(session, "SNAPSHOT_AUTO_DELETE") == "true"
    assert user_settings.get_bool(session, "SNAPSHOT_AUTO_DELETE") is True
    with pytest.raises(InvalidSetting, match="true or false"):
        save(session, "SNAPSHOT_AUTO_DELETE", "maybe")
    monkeypatch.setenv("SNAPSHOT_AUTO_DELETE", "false")
    assert user_settings.get_bool(session, "SNAPSHOT_AUTO_DELETE") is False


# --- Download sources and Soulseek's quality rules (ADR 0008) ---------------------------------


def test_list_settings_defaults_and_checks(session, monkeypatch):
    get_list = user_settings.get_list
    assert get_list(session, "DOWNLOAD_SOURCES") == ["slskd", "youtube"]
    assert get_list(session, "SLSKD_FORMATS") == ["flac", "aiff", "wav", "mp3"]
    assert get_int(session, "SLSKD_MIN_BITRATE") == 320

    save(session, "DOWNLOAD_SOURCES", " YouTube, slskd ")
    assert get(session, "DOWNLOAD_SOURCES") == "youtube,slskd"
    save(session, "DOWNLOAD_SOURCES", "none")  # nothing switched on: only link
    assert get_list(session, "DOWNLOAD_SOURCES") == []
    save(session, "DOWNLOAD_SOURCES", "")  # empty: back to the default
    assert get_list(session, "DOWNLOAD_SOURCES") == ["slskd", "youtube"]

    with pytest.raises(InvalidSetting, match="Unknown: torrent"):
        save(session, "DOWNLOAD_SOURCES", "youtube,torrent")
    with pytest.raises(InvalidSetting, match="only once"):
        save(session, "DOWNLOAD_SOURCES", "youtube,youtube")
    with pytest.raises(InvalidSetting, match="at least one"):
        save(session, "SLSKD_FORMATS", "none")  # Soulseek needs at least one format
    with pytest.raises(InvalidSetting, match="Unknown: wma"):
        save(session, "SLSKD_FORMATS", "flac,wma")
    with pytest.raises(InvalidSetting, match="Between 128 and 320"):
        save(session, "SLSKD_MIN_BITRATE", "96")
    save(session, "SLSKD_FORMATS", "mp3,flac")
    assert get_list(session, "SLSKD_FORMATS") == ["mp3", "flac"]

    monkeypatch.setenv("DOWNLOAD_SOURCES", "youtube")  # the env var wins and locks
    assert get_list(session, "DOWNLOAD_SOURCES") == ["youtube"] and is_locked("DOWNLOAD_SOURCES")
    monkeypatch.setenv("DOWNLOAD_SOURCES", "torrent")  # a bad env value: the default
    assert get_list(session, "DOWNLOAD_SOURCES") == ["slskd", "youtube"]


def test_every_downloader_is_a_download_source():
    from app.downloaders import DOWNLOADERS

    assert set(user_settings.BY_KEY["DOWNLOAD_SOURCES"].choices) == set(DOWNLOADERS)
    for name in user_settings.BY_KEY["SLSKD_FORMATS"].choices:
        assert name in user_settings.CHOICE_LABELS


def _list(page: str, key: str) -> list[tuple[str, bool]]:
    """The rows of a list setting on the page, in order: (name, switched on)."""
    block = re.search(rf'<fieldset id="list-{key}".*?</fieldset>', page, re.DOTALL).group(0)
    rows = re.findall(rf'name="{key}" value="(\w+)"\s+(checked)?', block)
    return [(name, bool(on)) for name, on in rows]


def test_settings_page_shows_the_download_sources(client):
    page = client.get("/settings").text
    assert page.index('id="sources"') < page.index('id="slskd"')  # near the top
    assert _list(page, "DOWNLOAD_SOURCES") == [("slskd", True), ("youtube", True)]
    assert "not connected" in page and "Add the address and API key below." in page
    summary = "Missing tracks are downloaded with Soulseek (skipped: not connected), then YouTube."
    assert summary in page
    assert _list(page, "SLSKD_FORMATS") == [
        ("flac", True), ("aiff", True), ("wav", True), ("mp3", True),
        ("m4a", False), ("ogg", False), ("opus", False),
    ]  # fmt: skip
    assert 'aria-label="Move YouTube up"' in page and 'hx-post="/partials/slskd-test"' in page
    assert 'name="SLSKD_MIN_BITRATE"' in page and 'placeholder="320"' in page

    client.put(
        "/api/settings",
        json={"SLSKD_URL": "http://192.168.1.10:5030", "SLSKD_API_KEY": "slskd-key-0042"},
    )
    page = client.get("/settings").text
    assert "not connected" not in page and "set up" in page
    assert "Missing tracks are downloaded with Soulseek, then YouTube." in page
    views = {v["key"]: v for v in client.get("/api/settings").json()}
    assert views["DOWNLOAD_SOURCES"]["choices"] == ["slskd", "youtube"]
    assert views["DOWNLOAD_SOURCES"]["value"] == "slskd,youtube"


def test_moving_and_switching_change_the_download_sources(client):
    key = "DOWNLOAD_SOURCES"
    shown = {f"shown_{key}": "1"}

    # ▲/▼ with htmx: the list comes back in its new order; nothing is saved yet.
    moved = client.post(
        f"/partials/settings-list/{key}",
        data={**shown, key: ["slskd", "youtube"], "move": f"{key}:youtube:up"},
    )
    assert _list(moved.text, key) == [("youtube", True), ("slskd", True)]
    assert "Not saved yet" in moved.text
    assert "Missing tracks are downloaded with YouTube, then Soulseek" in moved.text
    assert user_view(client, key)["value"] == "slskd,youtube"
    switched = client.post(f"/partials/settings-list/{key}", data={**shown, key: ["youtube"]})
    assert _list(switched.text, key) == [("youtube", True), ("slskd", False)]
    same = client.post(
        f"/partials/settings-list/{key}",
        data={**shown, key: ["slskd", "youtube"], "move": f"{key}:slskd:up"},  # already first
    )
    assert _list(same.text, key) == [("slskd", True), ("youtube", True)]
    assert "Not saved yet" not in same.text
    assert client.post("/partials/settings-list/NAVIDROME_URL", data={}).status_code == 404

    # "Save settings" saves the order on the page.
    response = client.post(
        "/settings", data={**shown, key: ["youtube", "slskd"]}, follow_redirects=False
    )
    assert response.status_code == 303
    assert user_view(client, key)["value"] == "youtube,slskd"
    assert _list(client.get("/settings").text, key) == [("youtube", True), ("slskd", True)]
    client.post("/settings", data={**shown, key: ["youtube"]})  # Soulseek switched off
    assert user_view(client, key)["value"] == "youtube"
    client.post("/settings", data=shown)  # everything off
    assert user_view(client, key)["value"] == "none"
    assert "No source is switched on" in client.get("/settings").text
    client.post("/settings", data={**shown, key: ["slskd", "youtube"]})  # the default again
    assert user_view(client, key)["source"] == "default"
    client.post("/settings", data={"NAVIDROME_USER": "admin"})  # a form without the list
    assert user_view(client, key)["value"] == "slskd,youtube"

    # Without JavaScript a ▲/▼ button sends the whole form, which saves the new order.
    response = client.post(
        "/settings",
        data={**shown, key: ["slskd", "youtube"], "move": f"{key}:slskd:down"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/settings?saved=1#list-{key}"
    assert user_view(client, key)["value"] == "youtube,slskd"


def test_soulseek_formats_and_bitrate_on_the_settings_page(client):
    key = "SLSKD_FORMATS"
    moved = client.post(
        f"/partials/settings-list/{key}",
        data={f"shown_{key}": "1", key: ["flac", "mp3", "opus"], "move": f"{key}:opus:up"},
    )
    assert [n for n, on in _list(moved.text, key) if on] == ["flac", "opus", "mp3"]
    form = {f"shown_{key}": "1", key: ["flac", "opus", "mp3"], "SLSKD_MIN_BITRATE": "256"}
    assert client.post("/settings", data=form, follow_redirects=False).status_code == 303
    assert user_view(client, key)["value"] == "flac,opus,mp3"
    assert user_view(client, "SLSKD_MIN_BITRATE")["value"] == "256"

    response = client.post("/settings", data={f"shown_{key}": "1"})  # every format off
    assert response.status_code == 422 and "Switch on at least one" in response.text
    assert user_view(client, key)["value"] == "flac,opus,mp3"  # not saved
    response = client.post("/settings", data={"SLSKD_MIN_BITRATE": "64"})
    assert response.status_code == 422 and "Between 128 and 320" in response.text


def test_an_env_var_locks_the_download_sources(client, monkeypatch):
    key = "DOWNLOAD_SOURCES"
    monkeypatch.setenv(key, "youtube")
    page = client.get("/settings").text
    block = re.search(rf'<fieldset id="list-{key}".*?</fieldset>', page, re.DOTALL).group(0)
    assert "set by the container" in block and f'name="shown_{key}"' not in block
    assert _list(page, key) == [("youtube", True), ("slskd", False)]
    assert block.count("disabled") == 2 + 2 * 2  # both switches, all four ▲/▼ buttons
    assert f"Env var: <code>{key}</code>" in page
    client.post("/settings", data={f"shown_{key}": "1", key: ["slskd"]})
    assert user_view(client, key)["value"] == "youtube"  # ignored
    assert client.put("/api/settings", json={key: "slskd"}).status_code == 409


def test_the_soulseek_test_button(client, monkeypatch):
    from app.downloaders import slskd
    from app.downloaders.base import DownloaderError

    calls = []
    answer = {"message": "slskd 0.22.5 answers; logged in to Soulseek as owner."}

    async def check_connection(url, api_key):
        calls.append((url, api_key))
        if isinstance(answer["message"], Exception):
            raise answer["message"]
        return answer["message"]

    monkeypatch.setattr(slskd, "check_connection", check_connection, raising=False)

    response = client.post("/partials/slskd-test", data={})
    assert "Enter slskd&#39;s address and API key first." in response.text and calls == []

    typed = {"SLSKD_URL": "http://192.168.1.10:5030/", "SLSKD_API_KEY": "typed-key-1234"}
    response = client.post("/partials/slskd-test", data=typed)
    assert calls == [("http://192.168.1.10:5030", "typed-key-1234")]
    assert 'class="result ok"' in response.text and "slskd 0.22.5 answers" in response.text

    client.put("/api/settings", json={"SLSKD_URL": "http://slskd:5030", "SLSKD_API_KEY": "s-0042"})
    client.post("/partials/slskd-test", data={"SLSKD_URL": "http://10.0.0.2:5030"})
    assert calls[-1] == ("http://10.0.0.2:5030", "s-0042")  # the saved key with the typed address

    answer["message"] = DownloaderError("slskd refused the API key (HTTP 401).")
    response = client.post("/partials/slskd-test", data={})
    assert 'class="result error"' in response.text and "refused the API key" in response.text
    assert calls[-1] == ("http://slskd:5030", "s-0042")
    answer["message"] = "Hello s-0042"  # a key in the answer is never shown
    assert "s-0042" not in client.post("/partials/slskd-test", data={}).text
    response = client.post("/partials/slskd-test", data={"SLSKD_URL": "slskd:5030"})
    assert "Address: Start with http:// or https://." in response.text

    answer["message"] = "slskd 0.22.5 answers."
    assert client.post("/api/slskd/test").json() == {"ok": True, "message": "slskd 0.22.5 answers."}


def user_view(client, key: str) -> dict:
    return {v["key"]: v for v in client.get("/api/settings").json()}[key]
