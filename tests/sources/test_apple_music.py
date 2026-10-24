"""Reading Apple Music playlist pages (saved pages, never online)."""

import json
import re

import httpx
import pytest
import respx

from app.sources.apple_music import AppleMusic
from app.sources.base import SourceError, detect_source

URL = "https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb"
REF = detect_source(URL)


def _page_json(page: str) -> dict:
    found = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', page, re.S)
    return json.loads(found.group(1))


def _with_json(page: str, data: dict) -> str:
    return re.sub(
        r'(<script[^>]*id="serialized-server-data"[^>]*>).*?(</script>)',
        lambda m: m.group(1) + json.dumps(data) + m.group(2),
        page,
        flags=re.S,
    )


@respx.mock
async def test_reads_a_playlist_page(saved, make):
    route = respx.get(URL).respond(text=saved.text("apple_music_playlist.html"))
    snapshot = await make(AppleMusic).fetch(REF)

    assert route.called
    assert snapshot.title == "Today’s Hits"
    assert snapshot.description.startswith("Today’s Hits is home to the biggest songs")
    assert "<b>" not in snapshot.description
    assert snapshot.cover_url.endswith("/1000x1000bb.jpg") and "{w}" not in snapshot.cover_url
    assert len(snapshot.tracks) == 4 and snapshot.warnings == []
    first = snapshot.tracks[0]
    assert (first.artist, first.title, first.album) == (
        "Olivia Rodrigo & Chappell Roan",
        "expectations",
        "expectations",  # Apple's "expectations - Single"
    )
    assert first.duration == 223.935
    assert first.source_id == "6822390426"
    assert first.isrc is None  # Apple's page has none
    assert (
        first.cover_url.startswith("https://is1-ssl.mzstatic.com/") and "{" not in first.cover_url
    )
    assert all(t.artist and t.title and t.duration for t in snapshot.tracks)


@respx.mock
async def test_says_when_the_page_lists_fewer_tracks_than_the_playlist_has(saved, make):
    page = saved.text("apple_music_playlist.html")
    data = _page_json(page)
    header = data["data"][0]["data"]["sections"][0]["items"][0]
    header["trackCount"] = 512
    respx.get(URL).respond(text=_with_json(page, data))
    snapshot = await make(AppleMusic).fetch(REF)
    assert snapshot.warnings == [
        "Apple Music's page lists 4 of the playlist's 512 tracks; Releasomat can only read those."
    ]


@respx.mock
async def test_reads_tracks_from_every_track_section(saved, make):
    page = saved.text("apple_music_playlist.html")
    data = _page_json(page)
    sections = data["data"][0]["data"]["sections"]
    track_list = next(s for s in sections if s["itemKind"] == "trackLockup")
    second = dict(track_list, id="track-list-2", items=track_list["items"][:1])
    sections.insert(sections.index(track_list) + 1, second)
    respx.get(URL).respond(text=_with_json(page, data))
    snapshot = await make(AppleMusic).fetch(REF)
    assert len(snapshot.tracks) == 5  # Explo kept only the last section


@respx.mock
async def test_page_changed_track_list_missing(saved, make):
    """The saved "changed" page has the sections, but no track list where it used to be."""
    respx.get(URL).respond(text=saved.text("apple_music_changed.html"))
    with pytest.raises(SourceError) as caught:
        await make(AppleMusic).fetch(REF)
    assert str(caught.value) == (
        "Apple Music changed its playlist page: Releasomat can't find the track list."
    )
    details = caught.value.details
    assert details["step"] == "read source" and details["url"] == URL
    assert any("songRowLockup" in found for found in details["found_sections"])
    assert 'itemKind "trackLockup"' in details["looked_for"]


@respx.mock
async def test_page_changed_script_missing(saved, make):
    page = saved.text("apple_music_playlist.html").replace(
        'id="serialized-server-data"', 'id="shoebox-media-api-cache"'
    )
    respx.get(URL).respond(text=page)
    with pytest.raises(SourceError, match="can't find the track list") as caught:
        await make(AppleMusic).fetch(REF)
    assert caught.value.details["found_scripts"] == ["shoebox-media-api-cache"]
    assert caught.value.details["page_title"] == "Today’s Hits - Playlist - Apple Music"
    assert len(caught.value.details["excerpt"]) <= 300


@respx.mock
async def test_page_changed_structure_missing(saved, make):
    respx.get(URL).respond(text=_with_json(saved.text("apple_music_playlist.html"), {"x": 1}))
    with pytest.raises(SourceError, match="can't find the track list") as caught:
        await make(AppleMusic).fetch(REF)
    assert caught.value.details["found"] == ["x"]


@respx.mock
async def test_page_with_broken_json(make):
    respx.get(URL).respond(text='<script id="serialized-server-data">{"data": [</script>')
    with pytest.raises(SourceError, match="can't find the track list") as caught:
        await make(AppleMusic).fetch(REF)
    assert caught.value.details["error_type"] == "JSONDecodeError"


@respx.mock
async def test_deleted_or_unshared_playlist(saved, make):
    respx.get(URL).respond(404, text=saved.text("apple_music_not_found.html"))
    with pytest.raises(SourceError, match="can't find this playlist") as caught:
        await make(AppleMusic).fetch(REF)
    assert caught.value.details["http_status"] == 404


@respx.mock
async def test_empty_playlist(saved, make):
    page = saved.text("apple_music_playlist.html")
    data = _page_json(page)
    for section in data["data"][0]["data"]["sections"]:
        if section["itemKind"] == "trackLockup":
            section["items"] = []
    respx.get(URL).respond(text=_with_json(page, data))
    with pytest.raises(SourceError, match="has no tracks"):
        await make(AppleMusic).fetch(REF)


@respx.mock
async def test_network_problems(make):
    respx.get(URL).mock(side_effect=httpx.ConnectError("Name or service not known"))
    with pytest.raises(SourceError, match="couldn't reach Apple Music") as caught:
        await make(AppleMusic).fetch(REF)
    assert caught.value.details["error_type"] == "ConnectError"
    respx.get(URL).respond(429)
    with pytest.raises(SourceError, match="limiting requests"):
        await make(AppleMusic).fetch(REF)
