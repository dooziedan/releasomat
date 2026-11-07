"""The Downloads page and /api/downloads (app/routes/downloads.py, app/download_folder.py):
the files Releasomat downloaded into its folder, their size, which ones no playlist uses any
more, and the owner's delete (ADR 0003): only after a confirmation page, only files Releasomat
downloaded, and the track becomes "Don't like"."""

from html import unescape

import pytest
from sqlmodel import Session, select
from test_integration_api import EPLE, GLUE, download, row

from app.db import get_engine
from app.models import Playlist, PlaylistTrack, utcnow
from app.templating import filesize


@pytest.fixture
def db(client, config):
    """A session on the app's database (created by the app's startup)."""
    with Session(get_engine(config.database_url)) as session:
        yield session


def leave_playlists(db, track_id: int) -> None:
    """The track leaves every playlist (it is in none now)."""
    for entry in db.exec(select(PlaylistTrack).where(PlaylistTrack.track_id == track_id)):
        entry.removed_at = utcnow()
        db.add(entry)
    db.commit()


def test_filesize():
    assert [filesize(n) for n in (0, 999, 1500, 8_400_000, 1_234_000_000, None)] == [
        "0 B",
        "999 B",
        "2 KB",
        "8.4 MB",
        "1.2 GB",
        "",
    ]


def test_the_folder_lists_only_downloads_that_are_there(client, db, config):
    music = config.music_dir
    eple = download(db, music, EPLE, title="Eple", size=300)
    glue = download(db, music, GLUE, title="Glue", playlist="Club", size=200)
    download(db, music, "Gone/Gone - Gone.mp3", title="Gone", write=False)  # not on disk
    download(db, music, None, title="Linked", status="in_library", navidrome_song_id="lib1")
    download(db, music, None, title="Adopted", status="adopted", former_path="A/A.opus",
             adopted_at=utcnow())  # fmt: skip
    (music / "Stray").mkdir()
    (music / "Stray" / "not-ours.mp3").write_bytes(b"x")  # Releasomat didn't download it
    leave_playlists(db, glue.track_id)

    answer = client.get("/api/downloads").json()
    assert (answer["count"], answer["size"]) == (2, 500)
    assert (answer["unused_count"], answer["unused_size"]) == (1, 200)
    assert [f["title"] for f in answer["files"]] == ["Glue", "Eple"]  # Bicep before Röyksopp
    by_title = {f["title"]: f for f in answer["files"]}
    mix = db.exec(select(Playlist).where(Playlist.title == "My Mix")).one()
    assert by_title["Eple"]["playlists"] == [{"id": mix.id, "title": "My Mix"}]
    assert by_title["Eple"]["id"] == eple.id and by_title["Eple"]["path"] == EPLE
    assert by_title["Eple"]["unused"] is False
    assert by_title["Glue"]["playlists"] == [] and by_title["Glue"]["unused"] is True
    assert by_title["Glue"]["downloaded_at"].startswith("2026-10-25T09:30:00")

    only = client.get("/api/downloads?unused=true").json()
    assert [f["title"] for f in only["files"]] == ["Glue"]
    assert only["count"] == 2  # the totals are for the whole folder


def test_the_downloads_page(client, db, config):
    music = config.music_dir
    eple = download(db, music, EPLE, title="Eple", size=8_400_000)
    glue = download(db, music, GLUE, title="Glue", size=1_000)
    leave_playlists(db, glue.track_id)

    page = unescape(client.get("/downloads").text)
    assert '<a href="/downloads" aria-current="page">Downloads</a>' in page  # the menu
    assert "8.4 MB" in page and "2 files" in page and "1 in no playlist now" in page
    assert EPLE in page and "FLAC 320 kbit/s" in page and "Soulseek" in page
    assert f'href="/downloads/{eple.id}/delete"' in page
    assert "In no playlist now" in page

    unused = unescape(client.get("/downloads?unused=true").text)
    assert "Glue" in unused and "Eple" not in unused
    assert f'href="/downloads/{glue.id}/delete?unused=true"' in unused
    assert "8.4 MB" in unused  # the totals stay


def test_the_empty_page(client):
    page = unescape(client.get("/downloads").text)
    assert "No downloads yet" in page and "In no playlist (" not in page


def test_delete_asks_first_and_changes_nothing_until_confirmed(client, db, config):
    music = config.music_dir
    eple = download(db, music, EPLE, title="Eple")
    page = unescape(client.get(f"/downloads/{eple.id}/delete").text)
    assert "Delete this file?" in page and EPLE in page and "My Mix" in page
    assert "This can't be undone." in page and "drop the song with their next refresh" in page
    assert f'action="/downloads/{eple.id}/delete"' in page
    assert (music / EPLE).is_file() and row(db, eple.id).status == "done"


def test_delete_from_the_page(client, db, config):
    music = config.music_dir
    eple = download(db, music, EPLE, title="Eple")
    download(db, music, GLUE, title="Glue")

    response = client.post(f"/downloads/{eple.id}/delete?unused=true", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/downloads?deleted=Eple&unused=true"
    assert not (music / EPLE).exists() and not (music / "Röyksopp").exists()  # empty folder too
    assert (music / GLUE).is_file()
    gone = row(db, eple.id)
    assert (gone.status, gone.disliked_reason, gone.path) == ("disliked", "deleted", None)
    assert gone.former_path == EPLE

    page = unescape(client.get(response.headers["location"]).text)
    assert "“Eple” was deleted and is now a Don't like track" in page
    assert "Undo on the Don't like page" in page

    # The same again: the file isn't there now; nothing else happens.
    again = client.post(f"/downloads/{eple.id}/delete")
    assert again.status_code == 404 and "isn't in Releasomat's folder now" in unescape(again.text)
    assert client.get(f"/downloads/{eple.id}/delete").status_code == 404


def test_delete_through_the_api(client, db, config):
    music = config.music_dir
    eple = download(db, music, EPLE, title="Eple")
    response = client.delete(f"/api/downloads/{eple.id}")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": eple.id,
        "path": EPLE,
        "still_in": ["My Mix"],
        "status": "disliked",
    }
    assert not (music / EPLE).exists()
    assert client.delete(f"/api/downloads/{eple.id}").status_code == 404
    assert client.delete("/api/downloads/9999").status_code == 404
    # Undo makes it a normal missing track again (the next refresh downloads it).
    assert client.delete(f"/api/tracks/{eple.track_id}/dislike").json()["status"] == "pending"


def test_only_files_releasomat_downloaded_are_deleted(client, db, config):
    music = config.music_dir
    # A row with a path that Releasomat didn't download (never offered, never deleted).
    theirs = download(db, music, EPLE, title="Eple", downloaded_by_releasomat=False)
    assert client.get("/api/downloads").json()["count"] == 0
    assert client.delete(f"/api/downloads/{theirs.id}").status_code == 404
    assert (music / EPLE).is_file()

    # A path that would leave the folder: refused, nothing done.
    outside = config.music_dir.parent / "outside.mp3"
    outside.write_bytes(b"x")
    sneaky = download(db, music, "../outside.mp3", title="Sneaky", write=False)
    assert client.delete(f"/api/downloads/{sneaky.id}").status_code == 404
    assert outside.is_file()
