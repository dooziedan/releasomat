"""Reading Beatport charts and Top 100 / Hype 100 lists (saved pages, never online)."""

import json
import re

import httpx
import pytest
import respx

from app.sources.base import SourceError, detect_source
from app.sources.beatport import Beatport

TOP = detect_source("https://www.beatport.com/top-100")
CHART = detect_source("https://www.beatport.com/chart/party-jumpin-chart/906471")
GENRE = detect_source("https://www.beatport.com/genre/house/5/hype-100")
FAKE_LOGIN = "FAKE-ANON-ACCESS-TOKEN-0123456789abcdef"  # in the saved pages' "anonSession"


def _with_json(page: str, change) -> str:
    """The page with its __NEXT_DATA__ changed by ``change(data)``."""
    found = re.search(r'(<script[^>]*id="__NEXT_DATA__"[^>]*>)(.*?)(</script>)', page, re.S)
    data = json.loads(found.group(2))
    change(data)
    return page[: found.start(2)] + json.dumps(data) + page[found.end(2) :]


@respx.mock
async def test_reads_the_top_100(saved, make):
    route = respx.get("https://www.beatport.com/top-100").respond(
        text=saved.text("beatport_top100.html")
    )
    snapshot = await make(Beatport).fetch(TOP)

    assert route.called
    assert snapshot.title == "Beatport Top 100" and snapshot.cover_url is None
    assert len(snapshot.tracks) == 4
    first = snapshot.tracks[0]
    assert first.artist == "Prospa, Kettama, Rohan Pinnock-Hamilton"
    assert first.title == "Think of You (Extended)"  # the mix name stays in the title
    assert (first.album, first.duration, first.isrc) == (
        "Think of You (Extended)",
        344.333,
        "CBEFB2607263",
    )
    assert (first.bpm, first.key, first.source_id) == (134.0, "Gb Minor", "30860950")
    assert first.cover_url == (
        "https://geo-media.beatport.com/image_size/1000x1000/1a863832-c1b9-4704-bcbd-549f677dc4f0.jpg"
    )
    assert all(t.isrc and t.bpm and t.key for t in snapshot.tracks)
    assert snapshot.tracks[1].title == "Fancy Footwork (Extended Mix)"


@respx.mock
async def test_reads_a_chart(saved, make):
    respx.get(CHART.url).respond(text=saved.text("beatport_chart.html"))
    snapshot = await make(Beatport).fetch(CHART)
    assert snapshot.title == "Party Jumpin Chart"
    assert snapshot.description == "A Beatport chart by NO STATIC."
    assert snapshot.cover_url.startswith("https://geo-media.beatport.com/image_size/1000x1000/")
    assert [t.title for t in snapshot.tracks] == [
        "Profill (Original Mix)",
        "Party Jumpin (feat. Max Cardona) (Extended Mix)",
        "Just A Feeling (Original Mix)",
    ]
    assert snapshot.tracks[1].artist == "Max Cardona, NO STATIC"


@respx.mock
async def test_reads_a_genre_list(saved, make):
    respx.get(GENRE.url).respond(text=saved.text("beatport_genre_hype100.html"))
    snapshot = await make(Beatport).fetch(GENRE)
    assert snapshot.title == "Beatport House Hype 100"
    assert len(snapshot.tracks) == 3


@respx.mock
async def test_says_when_the_page_has_only_the_first_tracks(saved, make):
    def more(data):
        for query in data["props"]["pageProps"]["dehydratedState"]["queries"]:
            if "results" in (query["state"]["data"] or {}):
                query["state"]["data"].update(count=150, next="api/…?page=2")

    respx.get(CHART.url).respond(text=_with_json(saved.text("beatport_chart.html"), more))
    snapshot = await make(Beatport).fetch(CHART)
    assert snapshot.warnings == ["Beatport's page lists 3 of 150 tracks."]


@respx.mock
async def test_page_changed_and_no_login_in_the_report(saved, make, report):
    def renamed(data):
        for query in data["props"]["pageProps"]["dehydratedState"]["queries"]:
            for track in (query["state"]["data"] or {}).get("results", []):
                track["title"] = track.pop("name")

    respx.get(TOP.url).respond(text=_with_json(saved.text("beatport_top100.html"), renamed))
    with pytest.raises(SourceError) as caught:
        await make(Beatport).fetch(TOP)
    assert str(caught.value) == "Beatport changed its pages: Releasomat can't find the track list."
    assert caught.value.details["found_queries"] == ["top-100-tracks-per_page=100"]
    assert FAKE_LOGIN not in report(caught.value)


@respx.mock
async def test_page_without_next_data(saved, make, report):
    page = saved.text("beatport_top100.html").replace('id="__NEXT_DATA__"', 'id="app-data"')
    respx.get(TOP.url).respond(text=page)
    with pytest.raises(SourceError, match="can't find the track list") as caught:
        await make(Beatport).fetch(TOP)
    assert caught.value.details["found_scripts"] == ["app-data"]
    assert FAKE_LOGIN not in report(caught.value)


@respx.mock
async def test_removed_chart_and_server_problems(make):
    respx.get(CHART.url).respond(404, text="<html><title>Page not found</title></html>")
    with pytest.raises(SourceError, match="can't find this chart") as caught:
        await make(Beatport).fetch(CHART)
    assert caught.value.details["http_status"] == 404
    respx.get(GENRE.url).respond(500)
    with pytest.raises(SourceError, match="Beatport has a problem right now"):
        await make(Beatport).fetch(GENRE)
    respx.get(TOP.url).mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(SourceError, match="couldn't reach Beatport"):
        await make(Beatport).fetch(TOP)


@respx.mock
async def test_empty_chart(saved, make):
    def empty(data):
        for query in data["props"]["pageProps"]["dehydratedState"]["queries"]:
            if "results" in (query["state"]["data"] or {}):
                query["state"]["data"]["results"] = []

    respx.get(CHART.url).respond(text=_with_json(saved.text("beatport_chart.html"), empty))
    with pytest.raises(SourceError, match="has no tracks"):
        await make(Beatport).fetch(CHART)
