"""Reading Tidal playlists through the official API (saved answers, never online).

The successful answers follow Tidal's API description (tidal-api-oas.json, version 1.10.169):
there were no credentials to capture real ones. The two error answers are real."""

import httpx
import pytest
import respx

from app.sources.base import PlaylistRef, SourceError
from app.sources.tidal import API, TOKEN_URL, Tidal

PLAYLIST = "36ea71a8-445e-41a4-82ab-6628c581535d"
REF = PlaylistRef(source="tidal", id=PLAYLIST, url=f"https://tidal.com/playlist/{PLAYLIST}")
CLIENT_ID = "tidal-client-id-0123"
SECRET = "tidal-client-secret-0123456789"
TOKEN = "FAKE-TIDAL-ACCESS-TOKEN-0123456789abcdef"  # in tidal_token.json
CREDENTIALS = {"TIDAL_CLIENT_ID": CLIENT_ID, "TIDAL_CLIENT_SECRET": SECRET}


def _tidal_answers(saved):
    """Routes for a whole successful read; returns them for checks."""
    token = respx.post(TOKEN_URL).respond(json=saved.json("tidal_token.json"))
    playlist = respx.get(f"{API}/playlists/{PLAYLIST}").respond(
        json=saved.json("tidal_playlist.json")
    )
    items = respx.get(f"{API}/playlists/{PLAYLIST}/relationships/items")
    page1, page2 = saved.json("tidal_items_page1.json"), saved.json("tidal_items_page2.json")
    items.side_effect = lambda request: httpx.Response(
        200, json=page2 if "page[cursor]" in request.url.params else page1
    )
    tracks = respx.get(f"{API}/tracks").respond(json=saved.json("tidal_tracks.json"))
    return token, playlist, items, tracks


@respx.mock
async def test_reads_a_playlist(saved, make):
    token, playlist, items, tracks = _tidal_answers(saved)
    snapshot = await make(Tidal, **CREDENTIALS).fetch(REF)

    # Logged in as the app, with the client ID and secret (HTTP Basic), not in the URL.
    login = token.calls.last.request
    assert login.content == b"grant_type=client_credentials"
    assert login.headers["Authorization"].startswith("Basic ")
    assert CLIENT_ID not in str(login.url) and SECRET not in str(login.url)
    # Every API request carries the token and the country.
    for route in (playlist, items, tracks):
        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["countryCode"] == "US"
    assert playlist.calls.last.request.url.params["include"] == "coverArt"
    assert [c.request.url.params.get("page[cursor]") for c in items.calls] == [None, "3nI1Esi"]
    asked = tracks.calls.last.request.url.params
    assert asked.get_list("filter[id]") == ["457581771", "77646169", "1971298", "99999999"]
    assert asked.get_list("include") == ["artists", "albums"]

    assert snapshot.title == "Electronic: Fixture Picks"
    assert snapshot.description == "Fresh electronic tracks."
    assert snapshot.cover_url.endswith("/1080x1080.jpg")  # the largest up to 1280 px
    # In the playlist's order, not in the order Tidal returned them.
    assert [t.title for t in snapshot.tracks] == [
        "Children (Dream Version)",
        "Get Lucky (Radio Edit)",
        "Windowlicker",
    ]
    lucky = snapshot.tracks[1]
    assert lucky.artist == "Daft Punk, Pharrell Williams"
    assert (lucky.album, lucky.duration, lucky.isrc) == ("Get Lucky", 248.0, "USQX91300108")
    assert (lucky.bpm, lucky.key, lucky.source_id) == (116.0, "F# Minor", "77646169")
    assert snapshot.tracks[2].key is None  # Tidal says UNKNOWN
    assert snapshot.warnings == [
        "Skipped 1 videos: Releasomat copies music only.",
        "1 tracks aren't in Tidal's catalogue for US.",
    ]


@respx.mock
async def test_the_login_is_reused_until_it_expires(saved, make):
    token, *_ = _tidal_answers(saved)
    source = make(Tidal, **CREDENTIALS)
    await source.fetch(REF)
    await source.fetch(REF)
    assert token.call_count == 1


async def test_needs_client_id_and_secret(make):
    with pytest.raises(SourceError) as caught:
        await make(Tidal, TIDAL_CLIENT_ID=CLIENT_ID).fetch(REF)
    assert str(caught.value) == "Tidal needs a client ID and secret: Settings → Tidal."
    assert caught.value.details == {"url": REF.url, "step": "read source"}


@respx.mock
async def test_wrong_client_id_or_secret(saved, make, report):
    """Tidal's real answer repeats the client ID; the report must not."""
    answer = saved.text("tidal_token_error.json").replace("fakeid", CLIENT_ID)
    respx.post(TOKEN_URL).respond(400, text=answer)
    with pytest.raises(SourceError, match="didn't accept the client ID and secret") as caught:
        await make(Tidal, **CREDENTIALS).fetch(REF)
    details = caught.value.details
    assert details["http_status"] == 400 and "invalid_grant" in details["excerpt"]
    assert CLIENT_ID not in report(caught.value) and SECRET not in report(caught.value)


@respx.mock
async def test_private_or_deleted_playlist(saved, make, report):
    respx.post(TOKEN_URL).respond(json=saved.json("tidal_token.json"))
    respx.get(f"{API}/playlists/{PLAYLIST}").respond(
        404, json={"errors": [{"code": "NOT_FOUND", "detail": "Playlist not found"}]}
    )
    with pytest.raises(SourceError, match="can't find this playlist") as caught:
        await make(Tidal, **CREDENTIALS).fetch(REF)
    assert caught.value.details["http_status"] == 404
    assert TOKEN not in report(caught.value) and SECRET not in report(caught.value)


@respx.mock
async def test_an_expired_login_is_dropped(saved, make):
    token = respx.post(TOKEN_URL).respond(json=saved.json("tidal_token.json"))
    respx.get(f"{API}/playlists/{PLAYLIST}").respond(
        401, text=saved.text("tidal_unauthorized.json")
    )
    with pytest.raises(SourceError, match="logs in again"):
        await make(Tidal, **CREDENTIALS).fetch(REF)
    _tidal_answers(saved)
    await make(Tidal, **CREDENTIALS).fetch(REF)
    assert token.call_count == 2  # logged in again


@respx.mock
async def test_rate_limit_and_server_problems(saved, make):
    respx.post(TOKEN_URL).respond(json=saved.json("tidal_token.json"))
    respx.get(f"{API}/playlists/{PLAYLIST}").respond(429, json={"errors": [{"code": "RATE"}]})
    with pytest.raises(SourceError, match="limiting requests"):
        await make(Tidal, **CREDENTIALS).fetch(REF)
    respx.get(f"{API}/playlists/{PLAYLIST}").respond(503)
    with pytest.raises(SourceError, match="Tidal has a problem right now"):
        await make(Tidal, **CREDENTIALS).fetch(REF)


@respx.mock
async def test_login_answer_without_token(make):
    respx.post(TOKEN_URL).respond(json={"token_type": "Bearer"})
    with pytest.raises(SourceError, match="had no token") as caught:
        await make(Tidal, **CREDENTIALS).fetch(REF)
    assert caught.value.details["found"] == ["token_type"]


@respx.mock
async def test_empty_playlist_and_changed_answers(saved, make):
    _tidal_answers(saved)
    respx.get(f"{API}/playlists/{PLAYLIST}/relationships/items").respond(
        json={"data": [], "links": {"self": "/x"}}
    )
    with pytest.raises(SourceError, match="has no tracks"):
        await make(Tidal, **CREDENTIALS).fetch(REF)

    respx.get(f"{API}/playlists/{PLAYLIST}/relationships/items").respond(json={"items": []})
    with pytest.raises(SourceError, match="doesn't understand") as caught:
        await make(Tidal, **CREDENTIALS).fetch(REF)
    assert caught.value.details["found"] == ["items"]
