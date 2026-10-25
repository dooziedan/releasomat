"""The Navidrome client: login, search, scans, finding downloads, playlists. Saved answers in
tests/fixtures/online/ (recorded from the dev Navidrome 0.64.2) stand in for a real Navidrome."""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from app import navidrome
from app.main import configure_logging
from app.navidrome import FoundSong, Navidrome, NavidromeError

ANSWERS = Path(__file__).parent / "fixtures" / "online"
URL = "http://navidrome.test:4533"


def answer(name: str) -> httpx.Response:
    return httpx.Response(200, json=json.loads((ANSWERS / name).read_text()))


def test_login_uses_a_salted_token_never_the_password():
    params = Navidrome(URL, "admin", "sesame").auth_params(salt="c19b2d")
    assert params["t"] == hashlib.md5(b"sesamec19b2d").hexdigest()
    assert params["s"] == "c19b2d" and params["u"] == "admin"
    assert "sesame" not in json.dumps(params)
    assert params["v"] == "1.16.1" and params["c"] == "releasomat" and params["f"] == "json"
    a, b = (
        Navidrome(URL, "admin", "sesame").auth_params(),
        Navidrome(URL, "admin", "sesame").auth_params(),
    )
    assert a["s"] != b["s"] and a["t"] != b["t"]  # a new salt for every request
    assert "sesame" not in repr(Navidrome(URL, "admin", "sesame"))


@respx.mock
async def test_ping_ok():
    route = respx.get(f"{URL}/rest/ping").mock(return_value=answer("navidrome_ping_ok.json"))
    result = await Navidrome(URL + "/", "admin", "sesame").ping()
    assert result.ok and result.message == "Connected to Navidrome 0.64.2 (abcdef0) as admin."
    request = route.calls.last.request
    assert request.headers["User-Agent"].startswith("Releasomat/")
    assert "sesame" not in str(request.url)


@respx.mock
async def test_ping_wrong_password():
    respx.get(f"{URL}/rest/ping").mock(return_value=answer("navidrome_ping_wrong_password.json"))
    result = await Navidrome(URL, "admin", "wrong").ping()
    assert not result.ok and result.message == "Wrong user name or password."


@respx.mock
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.ConnectError("refused"), "Can't reach http://navidrome.test:4533 (ConnectError)"),
        (httpx.ReadTimeout("slow"), "No answer from http://navidrome.test:4533 in time."),
        (httpx.Response(404), "answered with HTTP 404"),
        (httpx.Response(200, text="<html>Some other app</html>"), "doesn't answer like Navidrome"),
    ],
)
async def test_ping_problems_in_plain_words(response, message):
    route = respx.get(f"{URL}/rest/ping")
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        route.mock(return_value=response)
    result = await Navidrome(URL, "admin", "sesame").ping()
    assert not result.ok and message in result.message
    assert "sesame" not in result.message and "t=" not in result.message


async def test_ping_needs_the_settings_first():
    result = await Navidrome("", "", "").ping()
    assert not result.ok and "first" in result.message
    result = await Navidrome("navidrome:4533", "admin", "pw").ping()
    assert not result.ok and "http://" in result.message


@respx.mock
async def test_no_log_line_contains_the_token_or_salt(caplog, monkeypatch):
    """httpx logs every request address at INFO, and Subsonic addresses carry the token."""
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(navidrome.secrets, "token_hex", lambda n: "f00dfeed1234")
    token = hashlib.md5(b"sesamef00dfeed1234").hexdigest()
    respx.get(f"{URL}/rest/ping").mock(return_value=answer("navidrome_ping_ok.json"))
    assert (await Navidrome(URL, "admin", "sesame").ping()).ok
    respx.get(f"{URL}/rest/ping").mock(side_effect=httpx.ConnectError("refused"))
    assert not (await Navidrome(URL, "admin", "sesame").ping()).ok
    for record in caplog.records:
        line = record.getMessage()
        for secret in (token, "f00dfeed1234", "sesame"):
            assert secret not in line, f"{record.name} logged {line!r}"


def test_settings_test_button(client, monkeypatch):
    """Tries the typed values; an empty password field means the saved one."""
    client.put("/api/settings", json={"NAVIDROME_PASSWORD": "sesame"})
    with respx.mock:
        route = respx.get(f"{URL}/rest/ping").mock(return_value=answer("navidrome_ping_ok.json"))
        response = client.post(
            "/partials/navidrome-test",
            data={"NAVIDROME_URL": URL, "NAVIDROME_USER": "admin", "NAVIDROME_PASSWORD": ""},
        )
    assert "Connected to Navidrome 0.64.2" in response.text
    params = route.calls.last.request.url.params
    assert params["t"] == hashlib.md5(("sesame" + params["s"]).encode()).hexdigest()

    with respx.mock:
        respx.get(f"{URL}/rest/ping").mock(
            return_value=answer("navidrome_ping_wrong_password.json")
        )
        client.put("/api/settings", json={"NAVIDROME_URL": URL, "NAVIDROME_USER": "admin"})
        result = client.post("/api/navidrome/test").json()
    assert result == {
        "ok": False,
        "message": "Wrong user name or password.",
        "server_version": None,
    }


def nd(**kwargs) -> Navidrome:
    return Navidrome(URL, "admin", "sesame", **kwargs)


def song(id, title, artist, duration, **extra) -> dict:
    return {"id": id, "title": title, "artist": artist, "duration": duration, **extra}


def search_answer(*songs) -> httpx.Response:
    body = {"status": "ok", "searchResult3": {"song": list(songs)} if songs else {}}
    return httpx.Response(200, json={"subsonic-response": body})


@dataclass
class Wanted:
    artist: str
    title: str
    duration: float | None = None
    isrc: str | None = None
    mbid: str | None = None
    album: str | None = None


@respx.mock
async def test_search_reads_the_songs():
    route = respx.get(f"{URL}/rest/search3").mock(return_value=answer("navidrome_search_halo.json"))
    songs = await nd().search("halo", 50)
    assert [(s["artist"], s["title"], s["duration"]) for s in songs] == [
        ("Depeche Mode", "Halo", 270),
        ("Beyoncé", "Halo", 261),
    ]
    params = route.calls.last.request.url.params
    assert (params["query"], params["songCount"], params["albumCount"]) == ("halo", "50", "0")
    respx.get(f"{URL}/rest/search3").mock(return_value=answer("navidrome_search_empty.json"))
    assert await nd().search("nothing") == []


@respx.mock
async def test_find_in_library_by_name_picks_the_right_artist():
    """The demo library has two songs called Halo (Beyoncé and Depeche Mode)."""
    respx.get(f"{URL}/rest/search3").mock(return_value=answer("navidrome_search_halo.json"))
    found = await nd().find_in_library(Wanted("Beyonce", "Halo", 262), 85)
    assert found.song["artist"] == "Beyoncé" and found.method == "name"
    found = await nd().find_in_library(Wanted("Depeche Mode", "Halo (2006 Remaster)", 271), 85)
    assert found.song["artist"] == "Depeche Mode"
    assert await nd().find_in_library(Wanted("Beyoncé", "Halo", 400), 85) is None  # too long
    assert await nd().find_in_library(Wanted("Beyoncé", "Halo (Live)", 261), 85) is None


@respx.mock
async def test_find_in_library_by_mbid_first():
    mbid = "1a2b3c4d-1234-4abc-8def-0123456789ab"
    tagged = song("s1", "Some Other Title", "X", 100, musicBrainzId=mbid)
    route = respx.get(f"{URL}/rest/search3").mock(return_value=search_answer(tagged))
    found = await nd().find_in_library(Wanted("Artist", "Song", 300, mbid=mbid.upper()), 85)
    assert found.song["id"] == "s1" and found.method == "mbid"
    assert route.calls.last.request.url.params["query"] == mbid.upper()


@respx.mock
async def test_find_in_library_by_isrc():
    candidates = [
        song("a", "Strobe", "deadmau5", 600, isrc=["USUS11000001"]),
        song("b", "Strobe", "deadmau5", 637, isrc=["CAN111000100"]),
    ]
    respx.get(f"{URL}/rest/search3").mock(return_value=search_answer(*candidates))
    found = await nd().find_in_library(Wanted("deadmau5", "Strobe", None, isrc="CAN111000100"), 85)
    assert (found.song["id"], found.method) == ("b", "isrc")


@respx.mock
async def test_scan_waits_then_scans_releasomats_folder_then_waits():
    respx.get(f"{URL}/rest/getMusicFolders").mock(
        return_value=answer("navidrome_music_folders.json")
    )
    statuses = [
        answer("navidrome_scan_running.json"),  # someone else's scan: wait first
        answer("navidrome_scan_idle.json"),
        answer("navidrome_scan_running.json"),  # ours
        answer("navidrome_scan_idle.json"),
    ]
    status = respx.get(f"{URL}/rest/getScanStatus").mock(side_effect=statuses)
    start = respx.get(f"{URL}/rest/startScan").mock(
        return_value=answer("navidrome_scan_started.json")
    )
    assert await nd().scan(poll=0) == "folder"
    assert status.call_count == 4 and start.call_count == 1
    params = start.calls.last.request.url.params
    assert params.get_list("target") == ["1:releasomat"] and params["fullScan"] == "false"


@respx.mock
async def test_scan_falls_back_to_the_whole_library_and_explains_missing_rights():
    respx.get(f"{URL}/rest/getMusicFolders").mock(
        return_value=answer("navidrome_music_folders.json")
    )
    respx.get(f"{URL}/rest/getScanStatus").mock(return_value=answer("navidrome_scan_idle.json"))
    error = {"code": 10, "message": "unknown parameter target"}
    refused = httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": error}})
    start = respx.get(f"{URL}/rest/startScan").mock(
        side_effect=[refused, answer("navidrome_scan_started.json")]
    )
    assert await nd().scan(poll=0) == "library"
    assert "target" not in start.calls.last.request.url.params

    start.mock(return_value=answer("navidrome_not_admin.json"))
    with pytest.raises(NavidromeError, match="admin rights") as error:
        await nd().scan(poll=0)
    assert error.value.details == {
        "call": "startScan",
        "code": 50,
        "excerpt": "user not authorized",
    }


@respx.mock
async def test_scan_gives_up_after_its_time():
    respx.get(f"{URL}/rest/getScanStatus").mock(return_value=answer("navidrome_scan_running.json"))
    with pytest.raises(NavidromeError, match="didn't finish in time"):
        await nd().scan(timeout=0, poll=0)


@respx.mock
async def test_find_download_by_mbid_then_path_then_name():
    mbid = "1a2b3c4d-1234-4abc-8def-0123456789ab"
    navidrome = nd(subfolder="Releasomat")
    search = respx.get(f"{URL}/rest/search3").mock(
        return_value=search_answer(song("m", "Halo", "Beyoncé", 261, musicBrainzId=mbid))
    )
    found = await navidrome.find_download(
        Wanted("Beyoncé", "Halo", mbid=mbid), "B/B - Halo.opus", 85
    )
    assert found == FoundSong("m", "mbid")

    login = respx.post(f"{URL}/auth/login").mock(return_value=answer("navidrome_native_login.json"))
    native = [{"id": "p1", "path": "Releasomat/Beyoncé/Beyoncé - Halo.opus", "libraryId": 1}]
    songs = respx.get(f"{URL}/api/song").mock(return_value=httpx.Response(200, json=native))
    search.mock(return_value=search_answer())
    found = await navidrome.find_download(
        Wanted("Beyoncé", "Halo"), "Beyoncé/Beyoncé - Halo.opus", 85
    )
    assert found == FoundSong("p1", "path")
    request = songs.calls.last.request
    assert request.url.params["path"] == "Releasomat/Beyoncé/Beyoncé - Halo.opus"
    assert request.headers["X-ND-Authorization"] == "Bearer FAKE.JWT.TOKEN-for-tests"
    assert "sesame" in login.calls.last.request.content.decode()  # only the login sends it

    songs.mock(return_value=httpx.Response(200, json=[]))
    search.mock(return_value=answer("navidrome_search_halo.json"))
    found = await navidrome.find_download(Wanted("Beyoncé", "Halo", 261), "x.opus", 85)
    assert found == FoundSong("1SAbPINQ00E16hdqkuP8Dw", "name")

    songs.mock(return_value=httpx.Response(500))  # Navidrome's own API changed: by name anyway
    assert (await navidrome.find_download(Wanted("Beyoncé", "Halo"), "x.opus", 85)).method == "name"


@respx.mock
async def test_playlists_are_created_and_replaced_as_form_posts():
    create = respx.post(f"{URL}/rest/createPlaylist").mock(
        return_value=answer("navidrome_create_playlist.json")
    )
    ids = [f"song{n}" for n in range(300)]  # long lists: a form, not a giant address
    playlist_id = await nd().create_playlist("Weekly Jams", ids)
    assert playlist_id == "2jESo4alLvCLIG54wjv7gl"
    form = parse_qs(create.calls.last.request.content.decode())
    assert form["name"] == ["Weekly Jams"] and form["songId"] == ids
    assert "sesame" not in create.calls.last.request.content.decode()
    assert "songId" not in str(create.calls.last.request.url)

    await nd().replace_playlist(playlist_id, ["b", "a"])
    form = parse_qs(create.calls.last.request.content.decode())
    assert form["playlistId"] == [playlist_id] and form["songId"] == ["b", "a"]


@respx.mock
async def test_get_and_delete_playlists():
    get = respx.get(f"{URL}/rest/getPlaylist").mock(
        return_value=answer("navidrome_get_playlist.json")
    )
    playlist = await nd().get_playlist("2jESo4alLvCLIG54wjv7gl")
    assert [e["id"] for e in playlist["entry"]] == [
        "1SAbPINQ00E16hdqkuP8Dw",
        "4Y4X2lrdwFv5WXBn4YfiBC",
    ]
    get.mock(return_value=answer("navidrome_playlist_not_found.json"))
    assert await nd().get_playlist("gone") is None
    delete = respx.get(f"{URL}/rest/deletePlaylist").mock(
        return_value=answer("navidrome_delete_playlist.json")
    )
    await nd().delete_playlist("2jESo4alLvCLIG54wjv7gl")
    assert delete.calls.last.request.url.params["id"] == "2jESo4alLvCLIG54wjv7gl"


@respx.mock
async def test_errors_say_which_call_and_never_the_login():
    respx.get(f"{URL}/rest/search3").mock(return_value=httpx.Response(502, text="Bad gateway"))
    with pytest.raises(NavidromeError) as error:
        await nd().search("x")
    assert str(error.value) == f"{URL} answered with HTTP 502."
    assert error.value.details == {"call": "search3", "http_status": 502, "excerpt": "Bad gateway"}
    text = f"{error.value} {error.value.details}"
    assert "sesame" not in text and "t=" not in text and "s=" not in text
    with pytest.raises(NavidromeError, match="Settings"):
        await Navidrome("", "", "").search("x")


# --- Where a song really is (ADR 0009) --------------------------------------------------------
# Recorded from the dev Navidrome 0.64.2 with Releasomat's folder named "ReleasomatTest": the
# Eple download in that folder, the same song after the owner moved the file into Demo/ and
# Navidrome scanned (it keeps the ID), and a deleted download Navidrome marks missing.

EPLE_ID = "4JOFXagAW7gWM5pUcGBPjl"
EPLE = Wanted("Röyksopp", "Eple", 221)


def native(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=json.loads((ANSWERS / name).read_text()))


def area_nd() -> Navidrome:
    return nd(subfolder="ReleasomatTest")


def mock_native_login():
    return respx.post(f"{URL}/auth/login").mock(return_value=answer("navidrome_native_login.json"))


@respx.mock
async def test_song_info_tells_the_real_path_and_whether_it_is_missing():
    mock_native_login()
    route = respx.get(f"{URL}/api/song/{EPLE_ID}").mock(
        return_value=native("navidrome_native_song_area.json")
    )
    info = await area_nd().song_info(EPLE_ID)
    assert (info["path"], info["missing"]) == (
        "ReleasomatTest/Röyksopp/Röyksopp - Eple.flac",
        False,
    )
    assert (
        route.calls.last.request.headers["X-ND-Authorization"] == "Bearer FAKE.JWT.TOKEN-for-tests"
    )
    assert "sesame" not in str(route.calls.last.request.url)

    respx.get(f"{URL}/api/song/gone").mock(
        return_value=native("navidrome_native_song_not_found.json", 404)
    )
    assert await area_nd().song_info("gone") is None
    respx.get(f"{URL}/api/song/a%2Fb").mock(return_value=httpx.Response(500))
    with pytest.raises(NavidromeError, match="HTTP 500") as error:
        await area_nd().song_info("a/b")  # an ID never adds to the address
    assert error.value.details["call"] == "api/song/{id}"


@respx.mock
@pytest.mark.parametrize(
    ("recorded", "found"),
    [
        ("navidrome_native_song_area.json", False),  # still in Releasomat's folder (stale)
        ("navidrome_native_song_missing.json", False),  # deleted, Navidrome noticed
        ("navidrome_native_song_moved.json", True),  # moved into the library: same ID
    ],
)
async def test_find_elsewhere_checks_where_the_song_is(recorded, found):
    respx.get(f"{URL}/rest/search3").mock(return_value=answer("navidrome_search_eple.json"))
    mock_native_login()
    info = respx.get(f"{URL}/api/song/{EPLE_ID}").mock(return_value=native(recorded))
    result = await area_nd().find_elsewhere(EPLE, 85, skip_ids={EPLE_ID})
    assert (result is not None) is found
    if found:
        assert result.song["id"] == EPLE_ID and result.method == "name"
    assert info.call_count == 1


@respx.mock
async def test_find_elsewhere_takes_the_next_match_and_gives_up_after_three():
    recorded = json.loads((ANSWERS / "navidrome_search_eple.json").read_text())
    area_song = recorded["subsonic-response"]["searchResult3"]["song"][0]
    copies = [area_song] + [{**area_song, "id": f"copy{n}", "duration": 222} for n in range(4)]
    respx.get(f"{URL}/rest/search3").mock(return_value=search_answer(*copies))
    mock_native_login()
    area = json.loads((ANSWERS / "navidrome_native_song_area.json").read_text())
    moved = json.loads((ANSWERS / "navidrome_native_song_moved.json").read_text())
    checks = respx.get(url__regex=rf"{URL}/api/song/.+").mock(
        side_effect=lambda request: httpx.Response(
            200, json=moved if request.url.path.endswith("/copy0") else area
        )
    )
    result = await area_nd().find_elsewhere(EPLE, 85)
    assert result.song["id"] == "copy0"  # the copy in Releasomat's folder was passed over
    assert checks.call_count == 2

    checks.reset()
    checks.mock(return_value=httpx.Response(200, json=area))  # every copy is in the folder
    assert await area_nd().find_elsewhere(EPLE, 85) is None
    assert checks.call_count == 3  # gives up after three matches


@respx.mock
async def test_find_elsewhere_without_navidromes_own_api(caplog):
    """If Navidrome's own API doesn't answer, only the old songs of files that left the folder
    (``skip_ids``) are passed over, and that is logged at INFO."""
    caplog.set_level(logging.INFO, logger="app.navidrome")
    respx.get(f"{URL}/rest/search3").mock(return_value=answer("navidrome_search_eple.json"))
    respx.post(f"{URL}/auth/login").mock(return_value=httpx.Response(500))
    assert (await area_nd().find_elsewhere(EPLE, 85)).song["id"] == EPLE_ID
    assert await area_nd().find_elsewhere(EPLE, 85, skip_ids={EPLE_ID}) is None
    assert any("Can't check where song" in r.getMessage() for r in caplog.records)
    assert all(r.levelno == logging.INFO for r in caplog.records if r.name == "app.navidrome")


@respx.mock
async def test_find_in_library_checks_only_the_old_songs_of_files_that_left():
    respx.get(f"{URL}/rest/search3").mock(return_value=answer("navidrome_search_eple.json"))
    mock_native_login()
    info = respx.get(f"{URL}/api/song/{EPLE_ID}").mock(
        return_value=native("navidrome_native_song_area.json")
    )
    assert (await area_nd().find_in_library(EPLE, 85)).song["id"] == EPLE_ID  # unchecked
    assert info.call_count == 0
    assert await area_nd().find_in_library(EPLE, 85, skip_ids={EPLE_ID}) is None  # stale
    info.mock(return_value=native("navidrome_native_song_moved.json"))
    found = await area_nd().find_in_library(EPLE, 85, skip_ids={EPLE_ID})
    assert found.song["id"] == EPLE_ID  # Navidrome noticed the move: it is the library copy
