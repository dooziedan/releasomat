"""The integration API for Tagwerk (ADR 0009, app/routes/integration.py): optional and off
without a key, the key checked first and never echoed or logged, the answers' shapes, the path
spellings Tagwerk may send, every call idempotent, and no call touching a file except ``delete``.
Also the Settings group "Tagwerk" (optional, Generate, masking) and Undo for "Don't like"."""

import logging
import re
import unicodedata
from html import unescape
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app import reports
from app import settings as user_settings
from app.db import get_engine
from app.models import Playlist, PlaylistTrack, Track, TrackFile, utcnow

KEY = "tagwerk-test-key-0123456789"
AUTH = {"Authorization": f"Bearer {KEY}"}
EPLE = "Röyksopp/Röyksopp - Eple.opus"
GLUE = "Bicep/Bicep - Glue.flac"
POSTS = ("adopt", "returned", "discarded", "restored", "delete")


@pytest.fixture
def api(client, monkeypatch):
    """The app with the integration on (the key set by the container)."""
    monkeypatch.setenv("INTEGRATION_API_KEY", KEY)
    return client


@pytest.fixture
def db(client, config):
    """A session on the app's database (created by the app's startup)."""
    with Session(get_engine(config.database_url)) as session:
        yield session


def download(
    db, music: Path, path=EPLE, *, title="Eple", playlist="My Mix", size=100, write=True, **values
) -> TrackFile:
    """A track in a playlist with its file row: by default a file Releasomat downloaded into its
    folder (``write``: the file is created there too). ``path=None`` for a row whose file is
    elsewhere (``former_path``) or a song that was already in the library."""
    artist = (path or values.get("former_path") or "Artist/").split("/")[0]
    track = Track(artist=artist, title=title, album="Melody A.M.", duration=219,
                  isrc="GBAJE0100123", bpm=126.0, key="A Minor")  # fmt: skip
    db.add(track)
    db.commit()
    lists = db.exec(select(Playlist).where(Playlist.title == playlist)).all()
    mix = (
        lists[0]
        if lists
        else Playlist(source="deezer", source_id=playlist, url="x", title=playlist)
    )
    db.add(mix)
    db.commit()
    db.add(PlaylistTrack(playlist_id=mix.id, track_id=track.id, position=1))
    defaults = {
        "status": "done",
        "path": path,
        "downloaded_by_releasomat": path is not None,
        "navidrome_song_id": f"area-{title}",
        "downloader": "slskd",
        "format": (path or "x.opus").rsplit(".", 1)[-1],
        "bitrate": 320,
        "details": {"origin": "user1: x", "downloaded_at": "2026-10-25T09:30:00+00:00"},
    }
    file = TrackFile(track_id=track.id, **(defaults | values))
    db.add(file)
    db.commit()
    db.refresh(file)
    if file.path and write:
        target = music / file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
    return file


def post(client, call: str, *paths, headers=AUTH, **extra) -> dict:
    body = {"files": [p if isinstance(p, dict) else {"path": p} for p in paths], **extra}
    response = client.post(f"/api/integration/{call}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def answer(client, call: str, path, **extra) -> dict:
    """The answer for one file."""
    return post(client, call, path, **extra)["files"][0]


def row(db, file_id: int) -> TrackFile:
    db.expire_all()
    return db.get(TrackFile, file_id)


def snapshot(music: Path) -> dict:
    """Every file in the folder with its size and time: changes to any of them show up."""
    return {
        p.relative_to(music).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in music.rglob("*")
        if p.is_file()
    }


def extra_for(call: str) -> dict:
    return {"why": "move_failed"} if call == "returned" else {}


# --- Optional: off without a key --------------------------------------------------------------


def test_off_without_a_key(client):
    """No key set (the default): every call answers 403, with or without a key sent, and even
    with a body that doesn't fit (the key comes first)."""
    calls = [("get", "info"), ("get", "files")] + [("post", call) for call in POSTS]
    for method, call in calls:
        for headers in ({}, AUTH):
            response = client.request(method, f"/api/integration/{call}", headers=headers,
                                      json={"files": []} if method == "post" else None)  # fmt: skip
            assert response.status_code == 403, call
            assert response.json() == {
                "integration_api": 1,
                "error": "integration_off",
                "message": "The integration is off: Releasomat has no key for Tagwerk "
                "(Settings → Tagwerk). Releasomat works the same without it.",
            }
    assert client.post("/api/integration/adopt", json={"nope": 1}).status_code == 403
    malformed = client.post(
        "/api/integration/adopt", content=b"{", headers={"Content-Type": "application/json"}
    )
    assert malformed.status_code == 403


def test_a_key_that_breaks_the_rules_keeps_it_off(client, monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_KEY", "too-short")
    response = client.get("/api/integration/info", headers={"Authorization": "Bearer too-short"})
    assert response.status_code == 403
    assert "Off: the key is shorter than 16 characters" in client.get("/settings").text


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-key-0123456789"},
        {"Authorization": f"Bearer {KEY}x"},
        {"Authorization": f"Basic {KEY}"},
        {"Authorization": KEY},
        {"X-Api-Key": KEY},
    ],
)
def test_a_missing_or_wrong_key_is_401(api, headers):
    for method, call in [("get", "info"), ("get", "files"), ("post", "adopt")]:
        response = api.request(method, f"/api/integration/{call}", headers=headers,
                               json={"files": []} if method == "post" else None)  # fmt: skip
        assert response.status_code == 401, call
        assert response.json()["error"] == "wrong_key"
        assert response.headers["WWW-Authenticate"] == "Bearer"
    # Never in a URL: a key in the query string doesn't count.
    assert api.get(f"/api/integration/info?key={KEY}").status_code == 401


def test_no_422_before_the_key(api):
    """A body that doesn't fit gets 401 without the key, and the integration's shape with it."""
    for body in ({"nope": 1}, {"files": "x"}, {"files": [{"size": 3}]}):
        assert api.post("/api/integration/adopt", json=body).status_code == 401
    malformed = {"Content-Type": "application/json"}
    assert api.post("/api/integration/adopt", content=b"{", headers=malformed).status_code == 401

    response = api.post("/api/integration/adopt", json={"files": [{"size": 3}]}, headers=AUTH)
    assert response.status_code == 422
    data = response.json()
    assert (data["integration_api"], data["error"]) == (1, "bad_request")
    assert "files.0.path" in data["message"]
    response = api.post("/api/integration/adopt", content=b"{", headers=AUTH | malformed)
    assert response.status_code == 422 and response.json()["error"] == "bad_request"
    response = api.post("/api/integration/returned", json={"files": []}, headers=AUTH)
    assert response.status_code == 422 and "why" in response.json()["message"]
    response = api.post(
        "/api/integration/returned", json={"files": [], "why": "lost"}, headers=AUTH
    )
    assert response.status_code == 422


def test_unknown_calls_answer_in_the_same_shape_other_routes_keep_theirs(api):
    assert api.get("/api/integration/nope", headers=AUTH).json()["error"] == "not_found"
    response = api.get("/api/integration/adopt", headers=AUTH)
    assert response.status_code == 405 and response.json()["error"] == "method_not_allowed"
    # The rest of the app is unchanged.
    assert api.get("/api/nope").json() == {"detail": "Not Found"}
    response = api.post("/api/playlists", json={"url": 3})
    assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["body", "url"]


def test_the_key_never_appears_in_answers_logs_or_reports(api, db, config, caplog):
    caplog.set_level(logging.DEBUG)
    download(db, config.music_dir)
    texts = [
        api.get("/api/integration/info", headers=AUTH).text,
        api.get("/api/integration/files", headers=AUTH).text,
        api.post("/api/integration/adopt", json={"files": [{"path": EPLE}]}, headers=AUTH).text,
        api.get("/api/integration/info", headers={"Authorization": f"Bearer {KEY}-wrong"}).text,
        api.post("/api/integration/adopt", json={"x": 1}, headers=AUTH).text,
        api.get("/api/settings").text,
        api.get("/settings").text,
    ]
    for text in texts:
        assert KEY not in text
    assert KEY not in caplog.text
    # An error report scrubs it, as a known secret and as a header.
    assert KEY in reports.known_secrets(db)
    line = f"GET /api/integration/files Authorization: Bearer {KEY}"
    assert KEY not in reports.scrub(line, reports.known_secrets(db))
    assert KEY not in reports.scrub(line)  # even without knowing it


# --- info and files ---------------------------------------------------------------------------


def test_info(api, monkeypatch):
    data = api.get("/api/integration/info", headers=AUTH).json()
    assert data == {
        "app": "releasomat",
        "version": data["version"],
        "integration_api": 1,
        "folder": "Releasomat",
    }
    monkeypatch.setenv("NAVIDROME_SUBFOLDER", "Downloads")
    assert api.get("/api/integration/info", headers=AUTH).json()["folder"] == "Downloads"


def test_files_lists_the_files_in_the_area_now(api, db, config):
    music = config.music_dir
    nfd = unicodedata.normalize("NFD", EPLE)
    eple = download(db, music, nfd, size=1234)  # stored and on disk in NFD: answered in NFC
    glue = download(db, music, GLUE, title="Glue", playlist="Other", details={})
    gone = download(db, music, "Gone/Gone - Song.opus", title="Song")
    (music / gone.path).unlink()  # not on disk: not listed
    download(db, music, None, title="Adopted", status="adopted", former_path="A/A - Adopted.opus",
             downloaded_by_releasomat=False)  # fmt: skip
    download(db, music, None, title="Halo", status="in_library")  # a library song: never listed
    removed = db.exec(select(Playlist).where(Playlist.title == "Other")).one()
    db.add(PlaylistTrack(playlist_id=removed.id, track_id=eple.track_id, position=2))
    old = Playlist(source="deezer", source_id="old", url="x", title="Old")
    db.add(old)
    db.commit()
    db.add(PlaylistTrack(playlist_id=old.id, track_id=eple.track_id, position=1,
                         removed_at=eple.created_at))  # fmt: skip
    db.commit()

    data = api.get("/api/integration/files", headers=AUTH).json()

    assert data["integration_api"] == 1 and data["folder"] == "Releasomat"
    assert [f["id"] for f in data["files"]] == [glue.id, eple.id]  # by path
    first = data["files"][1]
    mix = db.exec(select(Playlist).where(Playlist.title == "My Mix")).one()
    assert first == {
        "id": eple.id,
        "path": EPLE,
        "size": 1234,
        "downloaded_at": "2026-10-25T09:30:00Z",
        "downloader": "slskd",
        "format": "opus",
        "bitrate": 320,
        "wanted": {
            "artist": unicodedata.normalize("NFD", "Röyksopp"),
            "title": "Eple",
            "album": "Melody A.M.",
            "duration": 219.0,
            "isrc": "GBAJE0100123",
            "mbid": None,
            "bpm": 126.0,
            "key": "A Minor",
        },
        "playlists": [
            {"id": mix.id, "name": "My Mix", "url": f"http://testserver/playlists/{mix.id}"},
            {"id": removed.id, "name": "Other", "url": f"http://testserver/playlists/{removed.id}"},
        ],  # not "Old": the track left it
    }
    assert data["files"][0]["downloaded_at"]  # without a stored time: the file's time


# --- Finding files by the paths Tagwerk sends -------------------------------------------------


def test_path_spellings(api, db, config):
    file = download(db, config.music_dir)
    nfd = unicodedata.normalize("NFD", EPLE)
    first = answer(api, "adopt", {"path": f"/music/Releasomat/{nfd}", "size": 100})
    assert first == {
        "asked": f"/music/Releasomat/{nfd}",
        "ok": True,
        "changed": True,
        "reason": None,
        "id": file.id,
        "path": EPLE,
        "status": "adopted",
    }
    for spelling in (EPLE, nfd, f"Releasomat/{EPLE}", f"/mnt/user/music/Releasomat/{EPLE}"):
        again = answer(api, "adopt", {"path": spelling, "id": file.id})
        assert (again["ok"], again["changed"], again["id"]) == (True, False, file.id), spelling

    for refused in ("../" + EPLE, "Röyksopp/../Röyksopp/x.opus", "", "/", "A//b.opus"):
        result = answer(api, "adopt", refused)
        assert result["ok"] is False and result["id"] is None, refused
        assert result["reason"]
    for unknown in ("/music/Other/" + EPLE, "Röyksopp/Röyksopp - Other.opus"):
        result = answer(api, "adopt", unknown)
        assert result["reason"] == "Releasomat doesn't know a file at this path.", unknown
    wrong_id = answer(api, "adopt", {"path": EPLE, "id": file.id + 100})
    assert wrong_id["ok"] is False and wrong_id["id"] is None


def test_answers_come_in_the_order_sent(api, db, config):
    eple = download(db, config.music_dir)
    glue = download(db, config.music_dir, GLUE, title="Glue")
    data = post(api, "adopt", GLUE, "Nope/x.mp3", EPLE)
    assert [(f["asked"], f["ok"], f["id"]) for f in data["files"]] == [
        (GLUE, True, glue.id), ("Nope/x.mp3", False, None), (EPLE, True, eple.id)
    ]  # fmt: skip


def test_two_rows_with_one_path_need_the_id(api, db, config):
    first = download(db, config.music_dir, None, status="adopted", former_path=EPLE)
    second = download(db, config.music_dir)  # a new download got the same name again
    result = answer(api, "discarded", EPLE)
    assert result["ok"] is False and str(first.id) in result["reason"]
    assert str(second.id) in result["reason"]
    result = answer(api, "discarded", {"path": EPLE, "id": first.id})
    assert (result["ok"], result["id"], result["status"]) == (True, first.id, "disliked")


# --- The notices: idempotent, database only ---------------------------------------------------


def test_adopt_and_returned(api, db, config):
    music = config.music_dir
    file = download(db, music)
    before = snapshot(music)

    assert answer(api, "adopt", EPLE)["changed"] is True
    assert row(db, file.id).status == "adopted" and row(db, file.id).adopted_at
    assert answer(api, "adopt", EPLE) | {"asked": None} == {
        "asked": None, "ok": True, "changed": False, "reason": None, "id": file.id, "path": EPLE,
        "status": "adopted",
    }  # fmt: skip

    back = answer(api, "returned", EPLE, why="move_failed")  # Tagwerk's move failed
    assert (back["ok"], back["changed"], back["status"]) == (True, True, "done")
    again = answer(api, "returned", EPLE, why="move_failed")
    assert (again["ok"], again["changed"]) == (True, False)
    restored = row(db, file.id)
    assert (restored.path, restored.downloaded_by_releasomat, restored.adopted_at) == (
        EPLE, True, None
    )  # fmt: skip
    assert restored.navidrome_song_id == "area-Eple"  # the song Navidrome still has
    assert snapshot(music) == before  # no call touched the file

    answer(api, "adopt", EPLE)
    given = answer(api, "returned", {"path": EPLE, "id": file.id}, why="given_back")
    assert (given["ok"], given["status"]) == (True, "done")
    assert row(db, file.id).navidrome_song_id is None  # found again after the next scan


def test_adopt_refusals(api, db, config):
    music = config.music_dir
    download(db, music, size=100)
    differs = answer(api, "adopt", {"path": EPLE, "size": 99})
    assert differs["ok"] is False and "another size" in differs["reason"]
    assert differs["status"] == "done"  # nothing changed

    download(db, music, None, title="Glue", status="disliked", former_path=GLUE,
             disliked_reason="deleted", disliked_at=utcnow())  # fmt: skip
    disliked = answer(api, "adopt", GLUE)
    assert disliked["ok"] is False and "Don't like" in disliked["reason"]
    not_adopted = answer(api, "returned", EPLE, why="move_failed")
    assert not_adopted["ok"] is True  # already a normal download: nothing to give back


def test_discarded_and_restored(api, db, config):
    music = config.music_dir
    file = download(db, music)
    answer(api, "adopt", EPLE)
    (music / EPLE).unlink()  # Tagwerk moved it into its inbox
    before = snapshot(music)

    thrown = answer(api, "discarded", {"path": EPLE, "id": file.id})
    assert (thrown["ok"], thrown["changed"], thrown["status"]) == (True, True, "disliked")
    assert row(db, file.id).disliked_reason == "discarded"
    assert answer(api, "discarded", EPLE)["changed"] is False  # repeated: ok again
    assert answer(api, "discarded", EPLE)["ok"] is True

    back = answer(api, "restored", EPLE)
    assert (back["ok"], back["changed"], back["status"]) == (True, True, "adopted")
    again = answer(api, "restored", EPLE)
    assert (again["ok"], again["changed"]) == (True, False)
    assert row(db, file.id).disliked_at is None
    assert snapshot(music) == before


def test_notices_for_files_that_were_never_adopted(api, db, config):
    download(db, config.music_dir)
    for call in ("discarded", "restored"):
        result = answer(api, call, EPLE)
        assert result["ok"] is False and result["reason"], call
        assert result["status"] == "done", call


def test_only_delete_touches_files(api, db, config):
    """adopt, returned, discarded and restored change only the database (ADR 0003)."""
    music = config.music_dir
    for n in range(4):
        download(db, music, f"Artist/Artist - Song {n}.opus", title=f"Song {n}")
    before = snapshot(music)
    paths = [f"Artist/Artist - Song {n}.opus" for n in range(4)]
    for call in ("adopt", "returned", "adopt", "discarded", "restored", "discarded"):
        post(api, call, *paths, **extra_for(call))
        assert snapshot(music) == before, call


def test_only_the_delete_call_uses_the_owners_delete():
    app = Path(__file__).resolve().parent.parent / "app"
    text = (app / "routes" / "integration.py").read_text()
    assert text.count("delete_and_dislike(") == 1
    assert "os.remove" not in text and ".unlink(" not in text and "shutil" not in text


# --- delete: the owner's delete, started in Tagwerk -------------------------------------------


def test_delete(api, db, config):
    music = config.music_dir
    file = download(db, music, size=100)
    keep = download(db, music, GLUE, title="Glue")

    deleted = answer(api, "delete", {"path": EPLE, "size": 100})
    assert (deleted["ok"], deleted["changed"], deleted["status"]) == (True, True, "disliked")
    assert not (music / EPLE).exists() and not (music / "Röyksopp").exists()
    gone = row(db, file.id)
    assert (gone.path, gone.former_path, gone.disliked_reason) == (None, EPLE, "deleted")
    again = answer(api, "delete", EPLE)
    assert (again["ok"], again["changed"]) == (True, False)  # repeated: ok, nothing touched
    assert (music / GLUE).exists() and row(db, keep.id).status == "done"


def test_delete_refusals(api, db, config, tmp_path):
    music = config.music_dir
    download(db, music, size=100)
    differs = answer(api, "delete", {"path": EPLE, "size": 5})
    assert differs["ok"] is False and "Nothing was changed" in differs["reason"]
    no_size = answer(api, "delete", EPLE)  # deleting can't be undone: the size is required
    assert no_size["ok"] is False and "size" in no_size["reason"]
    assert (music / EPLE).exists()

    download(db, music, GLUE, title="Glue")
    answer(api, "adopt", GLUE)
    adopted = answer(api, "delete", GLUE)
    assert adopted["ok"] is False and "Tagwerk adopted" in adopted["reason"]
    assert (music / GLUE).exists()
    answer(api, "discarded", GLUE)  # thrown away in Tagwerk's inbox: still not Releasomat's
    discarded = answer(api, "delete", GLUE)
    assert discarded["ok"] is False and "Tagwerk adopted" in discarded["reason"]

    outside = tmp_path / "precious.flac"
    outside.write_bytes(b"not Releasomat's")
    (music / "Evil").mkdir()
    (music / "Evil/Evil - Link.flac").symlink_to(outside)
    download(db, music, "Evil/Evil - Link.flac", title="Link", write=False)
    link = answer(api, "delete", "Evil/Evil - Link.flac")
    assert link["ok"] is False and link["status"] == "done"
    assert outside.read_bytes() == b"not Releasomat's"
    assert (music / "Evil/Evil - Link.flac").is_symlink()

    for unsafe in ("../precious.flac", "Evil/../../precious.flac", f"/music/Releasomat/../{EPLE}"):
        result = answer(api, "delete", unsafe)
        assert result["ok"] is False and result["id"] is None, unsafe
    assert outside.exists() and (music / EPLE).exists()


def test_delete_of_a_file_a_refresh_found_gone(api, db, config):
    """The owner's word replaces the guess; there is nothing to delete."""
    file = download(db, config.music_dir, None, status="disliked", former_path=EPLE,
                    disliked_reason="vanished", disliked_at=utcnow())  # fmt: skip
    result = answer(api, "delete", EPLE)
    assert (result["ok"], result["changed"]) == (True, True)
    assert row(db, file.id).disliked_reason == "deleted"


def test_delete_of_a_file_that_is_missing_changes_nothing(api, db, config):
    file = download(db, config.music_dir)
    (config.music_dir / EPLE).unlink()  # gone, and no refresh has looked yet (unmounted?)
    result = answer(api, "delete", EPLE)
    assert result["ok"] is False and "isn't there" in result["reason"]
    assert row(db, file.id).status == "done"


# --- /docs ------------------------------------------------------------------------------------


def test_docs_list_the_integration_routes_with_the_bearer_lock(client):
    spec = client.get("/openapi.json").json()
    for call in ("info", "files"):
        operation = spec["paths"][f"/api/integration/{call}"]["get"]
        assert operation["security"] == [{"TagwerkKey": []}], call
    for call in POSTS:
        operation = spec["paths"][f"/api/integration/{call}"]["post"]
        assert operation["security"] == [{"TagwerkKey": []}], call
        assert operation["tags"] == ["integration"]
        assert {"401", "403", "422"} <= set(operation["responses"]), call
    assert spec["components"]["securitySchemes"]["TagwerkKey"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Releasomat's key for Tagwerk (Settings → Tagwerk), sent as "
        "`Authorization: Bearer <key>`.",
    }
    tag = next(t for t in spec["tags"] if t["name"] == "integration")
    assert tag["description"].startswith("Optional, for Tagwerk. Off until a key is set")
    # Other routes need no key.
    assert "security" not in spec["paths"]["/api/playlists"]["get"]


# --- Settings → Tagwerk -----------------------------------------------------------------------


def tagwerk_group(page: str) -> str:
    return re.search(r'<article id="tagwerk">(.*?)</article>', page, re.DOTALL).group(1)


def test_the_tagwerk_group_is_optional_and_doesnt_nag(client):
    page = client.get("/settings").text
    group = tagwerk_group(page)
    assert "Optional. Only needed if you use Tagwerk to manage your library" in group
    assert "Releasomat works the same without it." in group
    assert "<dt>Integration</dt>" in group and "Off" in group
    assert "warn" not in group and "failed" not in group and "missing" not in group
    assert "Generate a key" in group
    assert "None since Releasomat started" in group
    assert "http://testserver" in group and "<code>Releasomat</code>" in group
    # Nothing about Tagwerk elsewhere on the page.
    assert page.count("Tagwerk") == page.count("Tagwerk", page.index('<article id="tagwerk">'))


def test_the_key_needs_16_characters_and_no_spaces(session):
    with pytest.raises(user_settings.InvalidSetting, match="At least 16 characters"):
        user_settings.save(session, "INTEGRATION_API_KEY", "short-key")
    with pytest.raises(user_settings.InvalidSetting, match="no spaces"):
        user_settings.save(session, "INTEGRATION_API_KEY", "a key with spaces inside")
    with pytest.raises(user_settings.InvalidSetting, match="no spaces"):
        user_settings.save(session, "INTEGRATION_API_KEY", "schlüssel-0123456789")
    user_settings.save(session, "INTEGRATION_API_KEY", f"  {KEY}  ")
    assert user_settings.get(session, "INTEGRATION_API_KEY") == KEY


def test_generate_fills_the_field_and_saves_nothing(client):
    response = client.post("/partials/integration-key")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    key = re.search(r'name="INTEGRATION_API_KEY" value="([^"]+)"', response.text).group(1)
    assert len(key) >= 43 and re.fullmatch(r"[A-Za-z0-9_-]+", key)
    assert "not saved yet" in response.text
    other = client.post("/partials/integration-key").text
    assert key not in other  # a new one each time
    view = {v["key"]: v for v in client.get("/api/settings").json()}["INTEGRATION_API_KEY"]
    assert view["is_set"] is False
    assert (
        client.get("/api/integration/info", headers={"Authorization": f"Bearer {key}"}).status_code
        == 403
    )

    # "Save settings" saves it; from then on it is masked and the integration is on.
    client.post("/settings", data={"INTEGRATION_API_KEY": key})
    page = client.get("/settings").text
    assert key not in page and f"Saved: ••••••••{key[-2:]}" in page
    assert '<span class="badge ok">on</span>' in tagwerk_group(page)
    assert key not in client.get("/api/settings").text
    assert (
        client.get("/api/integration/info", headers={"Authorization": f"Bearer {key}"}).status_code
        == 200
    )
    assert "(info)" in tagwerk_group(client.get("/settings").text)  # Tagwerk's last call

    # Its "Remove" box turns the integration off again.
    client.post("/settings", data={"clear_INTEGRATION_API_KEY": "on"})
    assert (
        client.get("/api/integration/info", headers={"Authorization": f"Bearer {key}"}).status_code
        == 403
    )


def test_a_too_short_key_is_not_saved(client):
    response = client.post("/settings", data={"INTEGRATION_API_KEY": "short"})
    assert response.status_code == 422 and "At least 16 characters" in response.text
    assert 'value="short"' not in response.text  # a secret is never sent back


def test_a_key_set_by_the_container(api):
    group = tagwerk_group(api.get("/settings").text)
    assert "key set by the container" in group and "Generate a key" not in group
    assert KEY not in group
    assert api.post("/partials/integration-key").status_code == 409


# --- Undo "Don't like" and the badges ---------------------------------------------------------


def test_undo_dont_like(client, db, config):
    file = download(db, config.music_dir, None, status="disliked", former_path=EPLE,
                    disliked_reason="deleted", disliked_at=utcnow())  # fmt: skip
    mix = db.exec(select(Playlist)).one()
    page = unescape(client.get(f"/playlists/{mix.id}").text)
    assert "Don't like" in page and "You deleted its file" in page and "Undo" in page
    tracks = client.get(f"/api/playlists/{mix.id}/tracks").json()[0]
    assert (tracks["disliked_reason"], tracks["former_path"]) == ("deleted", EPLE)
    assert tracks["disliked_at"] and tracks["adopted_at"] is None

    response = client.post(
        f"/playlists/{mix.id}/tracks/{file.track_id}/undo-dislike", headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert "<table" in response.text and "Don't like" not in unescape(response.text)
    assert ">Missing<" in response.text
    assert row(db, file.id).status == "pending"

    # The JSON route, idempotent.
    answer_ = client.delete(f"/api/tracks/{file.track_id}/dislike").json()
    assert answer_ == {"track_id": file.track_id, "changed": False, "status": "pending"}
    assert client.delete("/api/tracks/9999/dislike").status_code == 404


def test_undo_through_the_json_route_and_without_javascript(client, db, config):
    file = download(db, config.music_dir, None, status="disliked", former_path=EPLE,
                    disliked_reason="vanished", disliked_at=utcnow())  # fmt: skip
    result = client.delete(f"/api/tracks/{file.track_id}/dislike").json()
    assert result == {"track_id": file.track_id, "changed": True, "status": "pending"}
    mix = db.exec(select(Playlist)).one()
    response = client.post(
        f"/playlists/{mix.id}/tracks/{file.track_id}/undo-dislike", follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"].endswith("#tracks")


def test_badges_and_counts(client, db, config):
    music = config.music_dir
    now = utcnow()
    download(db, music, None, title="Adopted", status="adopted", former_path="A/A.opus",
             adopted_at=now)  # fmt: skip
    download(db, music, None, title="Thrown", status="disliked", former_path="T/T.opus",
             adopted_at=now, disliked_at=now, disliked_reason="discarded")  # fmt: skip
    download(db, music, None, title="Linked", status="in_library", navidrome_song_id="lib1",
             disliked_at=now, disliked_reason="deleted", former_path="L/L.opus")  # fmt: skip
    download(db, music, None, title="Imported", status="in_library", navidrome_song_id="lib2",
             adopted_at=now, former_path="I/I.opus")  # fmt: skip
    download(db, music, None, title="Moved", status="in_library", navidrome_song_id="lib3",
             former_path="M/M.opus", details={"moved": True})  # fmt: skip
    download(db, music, GLUE, title="Glue")
    mix = db.exec(select(Playlist)).one()

    page = unescape(client.get(f"/playlists/{mix.id}").text)
    assert page.count('class="badge track-adopted">Adopted by Tagwerk<') == 1
    assert page.count('class="badge track-disliked">Don\'t like<') == 2
    assert "You threw it away in Tagwerk" in page
    assert "Linked from your library, never downloaded." in page
    assert "adopted with Tagwerk" in page
    assert "you moved it there from Releasomat's folder" in page
    assert page.count("Undo<span") == 2  # every Don't like, also the linked one

    rows = unescape(client.get("/").text)
    assert "3 in library · 1 downloaded · 0 missing · 0 failed · 1 adopted · 1 Don't like" in rows


def test_the_playlist_list_says_nothing_about_tagwerk_without_it(client, db, config):
    download(db, config.music_dir)
    rows = unescape(client.get("/").text)
    assert "1 downloaded · 0 missing · 0 failed</small>" in rows
    assert "adopted" not in rows and "Don't like" not in rows
