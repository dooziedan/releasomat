"""Adding playlists, the playlist page's settings, and the same through the JSON API."""

from sqlmodel import Session

from app.config import get_config
from app.db import get_engine
from app.models import Playlist, PlaylistTrack, Track, TrackFile

DEEZER = "https://www.deezer.com/de/playlist/908622995?utm_campaign=share"


def _playlist(playlist_id: int) -> Playlist:
    with Session(get_engine(get_config().database_url)) as session:
        return session.get(Playlist, playlist_id)


def test_the_add_page_shows_the_detected_source(client):
    response = client.post("/partials/detect", data={"url": DEEZER})
    assert "Deezer playlist" in response.text and "908622995" in response.text
    response = client.post("/partials/detect", data={"url": "https://www.deezer.com/album/302127"})
    assert "an album on Deezer" in response.text
    assert client.post("/partials/detect", data={"url": ""}).text.strip() == ""


def test_add_by_link(client):
    response = client.post(
        "/playlists", data={"kind": "playlist", "url": DEEZER}, follow_redirects=False
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/playlists/") and location.endswith("?added=1")
    page = client.get(location).text
    assert "Deezer playlist 908622995" in page and "No tracks yet" in page
    assert "Choose a schedule below" in page

    again = client.post(
        "/playlists", data={"kind": "playlist", "url": DEEZER}, follow_redirects=False
    )
    assert again.headers["location"].endswith("?already=1")  # no second copy
    assert len(client.get("/api/playlists").json()) == 1


def test_add_with_a_wrong_link(client):
    response = client.post("/playlists", data={"kind": "playlist", "url": "https://example.com/x"})
    assert response.status_code == 422
    assert "know example.com" in response.text
    assert 'value="https://example.com/x"' in response.text


def test_follow_a_listenbrainz_series(client, monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_USER", "dooziedan")
    assert 'value="dooziedan"' in client.get("/playlists/new").text
    response = client.post(
        "/playlists",
        data={
            "kind": "series",
            "listenbrainz_series": "weekly-jams",
            "listenbrainz_user": "dooziedan",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    playlist = client.get("/api/playlists").json()[0]
    assert (playlist["source"], playlist["kind"], playlist["source_id"]) == (
        "listenbrainz", "series", "dooziedan/weekly-jams"
    )  # fmt: skip
    assert playlist["title"] == "Weekly Jams (dooziedan)"

    # Through the API, the user comes from the settings when it isn't given.
    response = client.post("/api/playlists", json={"listenbrainz_series": "daily-jams"})
    assert response.status_code == 201 and response.json()["source_id"] == "dooziedan/daily-jams"


def test_save_settings_on_the_playlist_page(client):
    playlist_id = client.post("/api/playlists", json={"url": DEEZER}).json()["id"]
    form = {
        "enabled": "on",
        "schedule_preset": "weekly",
        "schedule_weekday": "mon",
        "schedule_time": "06:30",
        "navidrome_mode": "snapshot",
        "snapshot_keep": "8",
    }
    response = client.post(f"/playlists/{playlist_id}/settings", data=form, follow_redirects=False)
    assert response.status_code == 303
    playlist = _playlist(playlist_id)
    assert playlist.schedule == "30 6 * * mon"
    assert playlist.next_refresh_at is not None  # the scheduler has a job for it now
    assert playlist.next_refresh_at.weekday() == 0
    assert (playlist.navidrome_mode, playlist.snapshot_keep) == ("snapshot", 8)
    page = client.get(f"/playlists/{playlist_id}").text
    assert "Delete their files" not in page and "removal" not in page  # ADR 0003: no such setting
    assert "Next refresh:" in page and 'value="30 6 * * mon"' not in page  # shown as a preset

    # Pausing removes the job.
    client.post(f"/playlists/{playlist_id}/settings", data={**form, "enabled": ""})
    assert _playlist(playlist_id).enabled is False
    assert _playlist(playlist_id).next_refresh_at is None
    assert "Paused" in client.get("/").text


def test_download_sources_are_global_not_per_playlist(client):
    """ADR 0008: the playlist page only says which sources are used; Settings decides."""
    playlist_id = client.post("/api/playlists", json={"url": DEEZER}).json()["id"]
    page = client.get(f"/playlists/{playlist_id}").text
    assert 'name="downloaders"' not in page and "Missing tracks are downloaded with" not in page
    assert "Downloads: Soulseek (skipped: not connected), then YouTube" in page
    assert 'href="/settings#sources"' in page

    client.put(
        "/api/settings",
        json={"SLSKD_URL": "http://192.168.1.10:5030", "SLSKD_API_KEY": "slskd-key-0042"},
    )
    assert "Downloads: Soulseek, then YouTube (" in client.get(f"/playlists/{playlist_id}").text
    client.put("/api/settings", json={"DOWNLOAD_SOURCES": "youtube"})
    assert "Downloads: YouTube (" in client.get(f"/playlists/{playlist_id}").text
    client.put("/api/settings", json={"DOWNLOAD_SOURCES": "none"})
    page = client.get(f"/playlists/{playlist_id}").text
    assert "Downloads: off, only songs already in Navidrome are linked" in page

    # The API has no per-playlist choice any more: sending one is an error, not ignored.
    assert "downloaders" not in client.get(f"/api/playlists/{playlist_id}").json()
    response = client.patch(f"/api/playlists/{playlist_id}", json={"downloaders": ["youtube"]})
    assert response.status_code == 422
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert "downloaders" not in schema["PlaylistChanges"]["properties"]


def test_invalid_settings_are_not_saved(client):
    playlist_id = client.post("/api/playlists", json={"url": DEEZER}).json()["id"]
    form = {"schedule_preset": "custom", "schedule_cron": "every monday"}
    response = client.post(f"/playlists/{playlist_id}/settings", data=form)
    assert response.status_code == 422
    assert "5 fields" in response.text
    assert _playlist(playlist_id).schedule == ""

    form = {"schedule_preset": "off", "navidrome_mode": "snapshot", "snapshot_keep": "0"}
    response = client.post(f"/playlists/{playlist_id}/settings", data=form)
    assert response.status_code == 422 and "Between 1 and 52" in response.text


def test_snapshots_to_keep_only_with_automatic_deleting(client):
    """Old snapshot playlists stay unless SNAPSHOT_AUTO_DELETE is on (owner's decision)."""
    playlist_id = client.post("/api/playlists", json={"url": DEEZER}).json()["id"]
    page = client.get(f"/playlists/{playlist_id}").text
    assert 'name="snapshot_keep"' not in page and "Old snapshots stay until you delete them" in page
    client.put("/api/settings", json={"SNAPSHOT_AUTO_DELETE": "true"})
    page = client.get(f"/playlists/{playlist_id}").text
    assert 'name="snapshot_keep"' in page and "Old snapshots stay" not in page


def test_schedule_fields_follow_the_preset(client):
    daily = client.post(
        "/partials/schedule", data={"schedule_preset": "daily", "schedule_time": "07:15"}
    )
    assert 'name="schedule_time"' in daily.text and 'name="schedule_weekday"' not in daily.text
    assert "Next refresh:" in daily.text and "07:15" in daily.text
    custom = client.post(
        "/partials/schedule", data={"schedule_preset": "custom", "schedule_cron": "0 6 * * 1-5"}
    )
    assert 'name="schedule_cron"' in custom.text and "Next refresh:" in custom.text
    broken = client.post(
        "/partials/schedule", data={"schedule_preset": "custom", "schedule_cron": "0 6"}
    )
    assert "5 fields" in broken.text
    switched = client.post(
        "/partials/schedule",
        data={"schedule_preset": "custom", "schedule_weekday": "fri", "schedule_time": "18:30"},
    )
    assert 'value="30 18 * * fri"' in switched.text  # starts from the weekly schedule shown before
    off = client.post("/partials/schedule", data={"schedule_preset": "off"})
    assert "only when you press Refresh now" in off.text


def test_api_settings(client):
    playlist_id = client.post("/api/playlists", json={"url": DEEZER}).json()["id"]
    response = client.patch(
        f"/api/playlists/{playlist_id}",
        json={"schedule": "0 */6 * * *", "navidrome_mode": "update"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schedule"] == "0 */6 * * *" and body["navidrome_mode"] == "update"
    assert body["next_refresh_at"] is not None

    bad = client.patch(f"/api/playlists/{playlist_id}", json={"schedule": "nope"})
    assert bad.status_code == 422
    assert set(bad.json()["detail"]) == {"schedule"}
    assert client.patch("/api/playlists/999", json={}).status_code == 404

    preview = client.get("/api/schedule/preview", params={"cron": "0 6 * * mon"}).json()
    assert preview["description"] == "Every Monday at 06:00" and preview["time_zone"] == "UTC"
    assert client.get("/api/schedule/preview", params={"cron": "x"}).status_code == 422


def test_api_detect_and_add_errors(client):
    detected = client.get("/api/sources/detect", params={"url": DEEZER}).json()
    assert detected["source"] == "deezer" and detected["source_label"] == "Deezer"
    assert client.get("/api/sources/detect", params={"url": "https://x.org/"}).status_code == 422
    assert client.post("/api/playlists", json={"url": "nonsense"}).status_code == 422
    assert client.post("/api/playlists", json={"url": DEEZER}).status_code == 201
    assert client.post("/api/playlists", json={"url": DEEZER}).status_code == 409


def test_remove_a_playlist(client, config):
    playlist_id = client.post("/api/playlists", json={"url": DEEZER}).json()["id"]
    downloaded = config.music_dir / "Artist" / "Artist - Song.opus"
    downloaded.parent.mkdir()
    downloaded.write_bytes(b"a download")
    with Session(get_engine(config.database_url)) as session:
        track = Track(artist="Artist", title="Song")
        session.add(track)
        session.commit()
        session.add(PlaylistTrack(playlist_id=playlist_id, track_id=track.id, position=1))
        session.add(TrackFile(track_id=track.id, path="Artist/Artist - Song.opus",
                              downloaded_by_releasomat=True, status="done"))  # fmt: skip
        session.commit()
    response = client.post(f"/playlists/{playlist_id}/delete", follow_redirects=False)
    assert response.headers["location"] == "/?removed=1"
    assert client.get(f"/playlists/{playlist_id}").status_code == 404
    assert client.get("/api/playlists").json() == []
    assert downloaded.read_bytes() == b"a download"  # ADR 0003: removing deletes no file
    playlist_id = client.post("/api/playlists", json={"url": DEEZER}).json()["id"]
    assert client.delete(f"/api/playlists/{playlist_id}").status_code == 204
    assert downloaded.exists()


def test_refresh_now_button(client):
    playlist_id = client.post("/api/playlists", json={"url": DEEZER}).json()["id"]
    response = client.post(f"/playlists/{playlist_id}/refresh", headers={"HX-Request": "true"})
    assert response.status_code == 200 and "<form" in response.text
    plain = client.post(f"/playlists/{playlist_id}/refresh", follow_redirects=False)
    assert plain.status_code == 303
