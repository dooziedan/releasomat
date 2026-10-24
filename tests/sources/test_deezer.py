"""Reading Deezer playlists through its public API (saved answers, never online)."""

import httpx
import pytest
import respx

from app.sources.base import PlaylistRef, SourceError
from app.sources.deezer import API, Deezer

SMALL = PlaylistRef(
    source="deezer", id="908622995", url="https://www.deezer.com/playlist/908622995"
)
LONG = PlaylistRef(
    source="deezer", id="1291349065", url="https://www.deezer.com/playlist/1291349065"
)


@respx.mock
async def test_reads_a_playlist_in_one_request(saved, make):
    playlist = respx.get(f"{API}/playlist/908622995").respond(
        json=saved.json("deezer_playlist.json")
    )
    pages = respx.get(f"{API}/playlist/908622995/tracks")
    snapshot = await make(Deezer).fetch(SMALL)

    assert playlist.call_count == 1 and not pages.called  # the tracks came with the playlist
    assert snapshot.title == "En mode 60"
    assert snapshot.description == "Les meilleurs hits des années 60 sont dans cette playlist."
    assert snapshot.cover_url.endswith("1000x1000-000000-80-0-0.jpg")
    assert len(snapshot.tracks) == 4
    first = snapshot.tracks[0]
    assert (first.artist, first.title, first.album) == (
        "The Beatles",
        "Hey Jude (Remastered 2015)",
        "1 (Remastered 2015)",
    )
    assert (first.duration, first.isrc, first.source_id) == (429.0, "GBUM71505902", "116348632")
    assert first.cover_url.endswith("1000x1000-000000-80-0-0.jpg")
    assert all(t.isrc for t in snapshot.tracks)


@respx.mock
async def test_long_playlists_are_read_page_by_page(saved, make, monkeypatch):
    """Deezer stops the list inside /playlist/<id> at 400 tracks; /tracks has them all."""
    monkeypatch.setattr("app.sources.deezer.PAGE_SIZE", 4)
    respx.get(f"{API}/playlist/1291349065").respond(json=saved.json("deezer_playlist_long.json"))
    page1, page2 = saved.json("deezer_tracks_page1.json"), saved.json("deezer_tracks_page2.json")
    pages = respx.get(f"{API}/playlist/1291349065/tracks")
    pages.side_effect = lambda request: httpx.Response(
        200, json=page1 if request.url.params["index"] == "0" else page2
    )
    snapshot = await make(Deezer).fetch(LONG)

    assert [dict(c.request.url.params) for c in pages.calls] == [
        {"index": "0", "limit": "4"},
        {"index": "4", "limit": "4"},
    ]
    assert len(snapshot.tracks) == 7
    assert snapshot.tracks[0].title == "The Times They Are A-Changin'"
    assert snapshot.title == "1001 Songs You Must Hear Before You Die"


@respx.mock
async def test_missing_isrc_and_odd_entries(saved, make):
    data = saved.json("deezer_playlist.json")
    del data["tracks"]["data"][0]["isrc"]
    data["tracks"]["data"][1]["artist"] = {}
    respx.get(f"{API}/playlist/908622995").respond(json=data)
    snapshot = await make(Deezer).fetch(SMALL)
    assert snapshot.tracks[0].isrc is None
    assert len(snapshot.tracks) == 3
    assert snapshot.warnings == ["Skipped 1 entries without artist or title."]


@respx.mock
async def test_deleted_or_private_playlist(saved, make):
    """Deezer answers 200 with {"error": {"code": 800, "message": "no data"}}."""
    respx.get(f"{API}/playlist/908622995").respond(json=saved.json("deezer_error_no_data.json"))
    with pytest.raises(SourceError, match="can't find this playlist") as caught:
        await make(Deezer).fetch(SMALL)
    assert caught.value.details["deezer_error"] == "DataException 800: no data"
    assert caught.value.details["step"] == "read source"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ({"type": "Exception", "message": "Quota limit exceeded", "code": 4}, "limiting requests"),
        ({"type": "Exception", "message": "Service busy", "code": 700}, "Deezer is busy"),
        ({"type": "OAuthException", "message": "Invalid OAuth access token.", "code": 300},
         "refused the request: Invalid OAuth access token"),
    ],
)  # fmt: skip
@respx.mock
async def test_api_errors(make, error, message):
    respx.get(f"{API}/playlist/908622995").respond(json={"error": error})
    with pytest.raises(SourceError, match=message):
        await make(Deezer).fetch(SMALL)


@respx.mock
async def test_error_on_a_later_page(saved, make, monkeypatch):
    monkeypatch.setattr("app.sources.deezer.PAGE_SIZE", 4)
    respx.get(f"{API}/playlist/1291349065").respond(json=saved.json("deezer_playlist_long.json"))
    pages = respx.get(f"{API}/playlist/1291349065/tracks")
    pages.side_effect = [
        httpx.Response(200, json=saved.json("deezer_tracks_page1.json")),
        httpx.Response(200, json={"error": {"type": "Exception", "message": "Quota", "code": 4}}),
    ]
    with pytest.raises(SourceError, match="limiting requests") as caught:
        await make(Deezer).fetch(LONG)
    assert caught.value.details["url"] == f"{API}/playlist/1291349065/tracks"


@respx.mock
async def test_empty_playlist_and_changed_answers(saved, make):
    data = saved.json("deezer_playlist.json")
    data["tracks"]["data"], data["nb_tracks"] = [], 0
    respx.get(f"{API}/playlist/908622995").respond(json=data)
    with pytest.raises(SourceError, match="has no tracks"):
        await make(Deezer).fetch(SMALL)

    respx.get(f"{API}/playlist/908622995").respond(json=["not", "a", "playlist"])
    with pytest.raises(SourceError, match="doesn't understand"):
        await make(Deezer).fetch(SMALL)


@respx.mock
async def test_server_problems(make):
    respx.get(f"{API}/playlist/908622995").respond(502, text="Bad Gateway")
    with pytest.raises(SourceError, match="Deezer has a problem right now") as caught:
        await make(Deezer).fetch(SMALL)
    assert caught.value.details["excerpt"] == "Bad Gateway"
    respx.get(f"{API}/playlist/908622995").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(SourceError, match="didn't answer in time"):
        await make(Deezer).fetch(SMALL)
