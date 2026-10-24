"""Reading ListenBrainz playlists and "Created for you" series (saved answers, never online)."""

import httpx
import pytest
import respx

from app.sources.base import PlaylistRef, SourceError
from app.sources.listenbrainz import API, ListenBrainz, series_ref

PLAYLIST = "55f2a01f-3974-46ca-8461-a8a84886eba4"  # rob's Weekly Exploration (saved)
REF = PlaylistRef(
    source="listenbrainz", id=PLAYLIST, url=f"https://listenbrainz.org/playlist/{PLAYLIST}/"
)
CREATED_FOR = f"{API}/user/rob/playlists/createdfor"
TOKEN = "lb-token-0123456789abcdef"


@respx.mock
async def test_reads_a_playlist(saved, make):
    route = respx.get(f"{API}/playlist/{PLAYLIST}").respond(
        json=saved.json("listenbrainz_playlist.json")
    )
    snapshot = await make(ListenBrainz).fetch(REF)

    assert snapshot.title == "Weekly Exploration for rob, week of 2026-08-24 Mon"
    assert snapshot.description.startswith("The ListenBrainz Weekly Exploration playlist")
    assert "<p>" not in snapshot.description
    assert snapshot.playlist_id is None  # only for series
    assert len(snapshot.tracks) == 4
    first = snapshot.tracks[0]
    assert (first.artist, first.title, first.album, first.duration) == (
        "Dauwd",
        "Lydia",
        "Connecting The Dots",
        348.0,
    )
    assert first.mbid == first.source_id == "dad4820e-4887-4d1e-83e2-6fe1166b3f49"
    assert first.cover_url == (
        "https://coverartarchive.org/release/5828cd29-f48b-40cf-8948-df723d8a87fa/31380275915-500.jpg"
    )
    assert snapshot.cover_url == first.cover_url
    assert all(t.mbid for t in snapshot.tracks)
    assert "Authorization" not in route.calls.last.request.headers  # no token set


@respx.mock
async def test_sends_the_token_from_settings(saved, make):
    route = respx.get(f"{API}/playlist/{PLAYLIST}").respond(
        json=saved.json("listenbrainz_playlist.json")
    )
    await make(ListenBrainz, LISTENBRAINZ_TOKEN=TOKEN).fetch(REF)
    assert route.calls.last.request.headers["Authorization"] == f"Token {TOKEN}"


@respx.mock
async def test_series_takes_the_newest_playlist_of_its_kind_across_pages(saved, make, monkeypatch):
    monkeypatch.setattr("app.sources.listenbrainz.PAGE_SIZE", 3)
    page1, page2 = (saved.json(f"listenbrainz_createdfor_page{n}.json") for n in (1, 2))
    page1["playlist_count"] = page2["playlist_count"] = 6  # the saved user has 46; stop after 2
    pages = respx.get(CREATED_FOR)
    pages.side_effect = lambda request: httpx.Response(
        200, json=page1 if request.url.params["offset"] == "0" else page2
    )
    playlist = respx.get(f"{API}/playlist/{PLAYLIST}").respond(
        json=saved.json("listenbrainz_playlist.json")
    )

    snapshot = await make(ListenBrainz).fetch(series_ref("rob", "weekly-exploration"))

    assert pages.call_count == 2  # page 2 has an older Weekly Exploration: it isn't taken
    assert [c.request.url.params["offset"] for c in pages.calls] == ["0", "3"]
    assert playlist.called
    assert snapshot.playlist_id == PLAYLIST
    assert snapshot.title.startswith("Weekly Exploration for rob")


@respx.mock
async def test_series_picks_by_date_not_by_position(saved, make):
    page = saved.json("listenbrainz_createdfor_page2.json")  # Weekly Jams 08-16, WE 07-28, …
    newer = saved.json("listenbrainz_createdfor_page1.json")["playlists"]  # DJ, WE, WJ 08-23
    page["playlists"] = page["playlists"] + newer  # newest last
    page["playlist_count"] = len(page["playlists"])
    respx.get(CREATED_FOR).respond(json=page)
    newest_jams = "54cc305b-f68e-4ab8-a2b9-3188727a3b5b"
    route = respx.get(f"{API}/playlist/{newest_jams}").respond(
        json=saved.json("listenbrainz_playlist.json")
    )
    snapshot = await make(ListenBrainz).fetch(series_ref("rob", "weekly-jams"))
    assert route.called and snapshot.playlist_id == newest_jams


@respx.mock
async def test_series_not_made_yet(make):
    respx.get(f"{API}/user/newbie/playlists/createdfor").respond(
        json={"count": 0, "offset": 0, "playlist_count": 0, "playlists": []}
    )
    with pytest.raises(SourceError, match="no Daily Jams playlist for newbie yet") as caught:
        await make(ListenBrainz).fetch(series_ref("newbie", "daily-jams"))
    assert "troi-bot" in str(caught.value)
    assert caught.value.details["step"] == "read source"


@respx.mock
async def test_unknown_user(make):
    respx.get(f"{API}/user/nobody/playlists/createdfor").respond(
        404, json={"code": 404, "error": "Cannot find user: nobody"}
    )
    with pytest.raises(SourceError, match="doesn't know the user “nobody”") as caught:
        await make(ListenBrainz).fetch(series_ref("nobody", "weekly-jams"))
    assert caught.value.details["http_status"] == 404
    assert "Cannot find user" in caught.value.details["excerpt"]


@pytest.mark.parametrize(
    ("token", "hint"), [("", "add your user token in Settings"), (TOKEN, "it is private.")]
)
@respx.mock
async def test_deleted_or_private_playlist(make, report, token, hint):
    respx.get(f"{API}/playlist/{PLAYLIST}").respond(
        404, json={"code": 404, "error": f"Cannot find playlist: {PLAYLIST}"}
    )
    with pytest.raises(SourceError, match="can't find this playlist") as caught:
        await make(ListenBrainz, LISTENBRAINZ_TOKEN=token).fetch(REF)
    assert hint in str(caught.value)
    assert caught.value.details["url"] == f"{API}/playlist/{PLAYLIST}"
    assert TOKEN not in report(caught.value)


@respx.mock
async def test_wrong_token(make, report):
    respx.get(f"{API}/playlist/{PLAYLIST}").respond(
        401, json={"code": 401, "error": f"Invalid authorization token {TOKEN}."}
    )
    with pytest.raises(SourceError, match="didn't accept the user token") as caught:
        await make(ListenBrainz, LISTENBRAINZ_TOKEN=TOKEN).fetch(REF)
    assert caught.value.details["http_status"] == 401
    assert TOKEN not in report(caught.value)  # not even when ListenBrainz repeats it


@respx.mock
async def test_down_or_slow_after_three_tries(make):
    route = respx.get(f"{API}/playlist/{PLAYLIST}")
    route.side_effect = [httpx.Response(503), httpx.ReadTimeout("slow"), httpx.Response(502)]
    with pytest.raises(SourceError, match="ListenBrainz is down or very slow") as caught:
        await make(ListenBrainz).fetch(REF)
    assert route.call_count == 3
    assert caught.value.details["http_status"] == 502 and caught.value.details["tries"] == 3


@respx.mock
async def test_a_retry_that_works(saved, make):
    route = respx.get(f"{API}/playlist/{PLAYLIST}")
    route.side_effect = [
        httpx.ConnectError("reset"),
        httpx.Response(200, json=saved.json("listenbrainz_playlist.json")),
    ]
    assert len((await make(ListenBrainz).fetch(REF)).tracks) == 4


@respx.mock
async def test_rate_limit(make):
    respx.get(f"{API}/playlist/{PLAYLIST}").respond(
        429, headers={"X-RateLimit-Reset-In": "55"}, json={"code": 429, "error": "Too many"}
    )
    with pytest.raises(SourceError, match="limiting requests") as caught:
        await make(ListenBrainz).fetch(REF)
    assert caught.value.details["retry_after"] == 55


@respx.mock
async def test_empty_playlist(saved, make):
    data = saved.json("listenbrainz_playlist.json")
    data["playlist"]["track"] = []
    respx.get(f"{API}/playlist/{PLAYLIST}").respond(json=data)
    with pytest.raises(SourceError, match="has no tracks"):
        await make(ListenBrainz).fetch(REF)


@respx.mock
async def test_answer_changed(make):
    respx.get(f"{API}/playlist/{PLAYLIST}").respond(json={"jspf": {"tracks": []}})
    with pytest.raises(SourceError, match="doesn't understand") as caught:
        await make(ListenBrainz).fetch(REF)
    assert caught.value.details["found"] == ["jspf"]


@respx.mock
async def test_tracks_without_title_are_skipped_with_a_warning(saved, make):
    data = saved.json("listenbrainz_playlist.json")
    del data["playlist"]["track"][1]["title"]
    respx.get(f"{API}/playlist/{PLAYLIST}").respond(json=data)
    snapshot = await make(ListenBrainz).fetch(REF)
    assert len(snapshot.tracks) == 3
    assert snapshot.warnings == ["Skipped 1 tracks without artist or title."]


async def test_reads_the_token_from_the_saved_settings(engine, saved, monkeypatch):
    """Without a test setting, a source reads user settings like the rest of Releasomat."""
    from sqlmodel import Session

    from app import settings

    with Session(engine) as session:
        settings.save(session, "LISTENBRAINZ_TOKEN", TOKEN)
    with respx.mock:
        route = respx.get(f"{API}/playlist/{PLAYLIST}").respond(
            json=saved.json("listenbrainz_playlist.json")
        )
        await ListenBrainz().fetch(REF)
    assert route.calls.last.request.headers["Authorization"] == f"Token {TOKEN}"
