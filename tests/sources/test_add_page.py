"""The Add playlist page shows every source, and the detected link names its source."""

import re

from app.sources import SOURCES


def test_the_add_page_lists_every_source_with_examples(client):
    page = client.get("/playlists/new").text
    chips = re.findall(r'class="source-chip" data-source="([a-z_]+)"', page)
    assert chips == list(SOURCES)  # in SOURCES' order, ListenBrainz first, Beatport last
    for source in SOURCES.values():
        assert f'id="example-{source.type}" hidden' in page
        for example in source.examples:
            assert f"<code>{example}</code>" in page
    assert "Settings → Tidal" in page
    assert "Which links work?" not in page  # no longer hidden away
    assert "official" not in page.lower() and "spotify" not in page.lower()
    assert "Created for you" in page and 'name="listenbrainz_series"' in page
    assert "color-mix" not in page


def test_the_detected_link_names_its_source(client):
    response = client.post(
        "/partials/detect", data={"url": "https://www.beatport.com/genre/house/5/top-100"}
    )
    assert 'data-source="beatport"' in response.text
    assert "Beatport genre Top 100" in response.text
    response = client.post("/partials/detect", data={"url": "https://open.spotify.com/playlist/x"})
    assert "data-source" not in response.text and "know open.spotify.com" in response.text
    assert client.post("/partials/detect", data={"url": ""}).text.strip() == ""
