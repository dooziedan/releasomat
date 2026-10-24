"""The helpers every source uses: requests with plain-language errors, excerpts without secrets,
JSON out of web pages, small conversions, and video titles."""

import re

import httpx
import pytest
import respx

from app.httpclient import client
from app.sources import listenbrainz
from app.sources.base import SourceError
from app.sources.titles import clean_title, split_artist_title
from app.sources.web import (
    excerpt,
    get_json,
    html_to_text,
    iso_seconds,
    ms_to_seconds,
    request,
    script_ids,
    script_json,
    scrub,
    source_error,
)

URL = "https://api.example.org/playlist/1"


def test_excerpt_is_short_on_one_line_and_without_secrets():
    text = "line one\n\n   line two " + "x" * 400
    short = excerpt(text)
    assert len(short) == 300 and short.startswith("line one line two") and short.endswith("…")
    assert excerpt(b"bytes \xff too") == "bytes � too"
    assert excerpt(None) == ""


@pytest.mark.parametrize(
    "text",
    [
        '{"access_token":"eyJhbGciOiJIUzI1NiJ9.abcdefgh"}',
        "client_secret=s3cr3t-value-123&grant_type=client_credentials",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdefgh",
        "Token 0123456789abcdef0123",
        "cookie: SESSION=abcdefghijk",
    ],
)
def test_scrub_hides_anything_that_looks_like_a_secret(text):
    hidden = scrub(text)
    assert "•••" in hidden
    for secret in ("eyJhbGciOiJIUzI1NiJ9", "s3cr3t-value-123", "0123456789abcdef0123", "SESSION"):
        assert secret not in hidden


def test_scrub_hides_known_values_and_keeps_normal_text():
    assert scrub("id=my-client-id here", hide=("my-client-id", None, "")) == "id=••• here"
    assert scrub('{"token": null, "userTokenHash": ""}') == '{"token": null, "userTokenHash": ""}'


def test_source_error_details_for_the_report():
    error = source_error(
        "Something broke.",
        url=URL,
        http_status=None,
        excerpt="secret-value " * 50,
        found=["a", "secret-value"],
        hide=("secret-value",),
    )
    assert isinstance(error, SourceError) and str(error) == "Something broke."
    assert error.details["step"] == "read source"
    assert "http_status" not in error.details  # empty details are left out
    assert len(error.details["excerpt"]) <= 300 and "secret-value" not in error.details["excerpt"]
    assert error.details["found"] == ["a", "•••"]
    assert source_error("x", step="match").details["step"] == "match"


@respx.mock
async def test_request_returns_successful_answers():
    respx.get(URL).respond(json={"ok": True})
    async with client() as http:
        assert await get_json(http, URL, service="Example") == {"ok": True}
    assert respx.calls.last.request.headers["User-Agent"].startswith("Releasomat/")


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (404, "Example can't find this playlist"),
        (429, "Example is limiting requests"),
        (500, "Example has a problem right now (HTTP 500)"),
        (503, "Example has a problem right now (HTTP 503)"),
        (400, "Example refused the request (HTTP 400)"),
    ],
)
@respx.mock
async def test_error_answers_have_plain_messages_and_details(status, message):
    respx.get(URL).respond(status, text='{"error": "nope", "access_token": "abcdefghijklmnop"}')
    async with client() as http:
        with pytest.raises(SourceError, match=re.escape(message)) as caught:
            await request(http, "GET", URL, service="Example")
    details = caught.value.details
    assert details["http_status"] == status and details["url"] == URL
    assert '"error": "nope"' in details["excerpt"] and "abcdefghijklmnop" not in details["excerpt"]


@respx.mock
async def test_own_messages_per_status():
    respx.get(URL).respond(403)
    async with client() as http:
        with pytest.raises(SourceError, match="private"):
            await request(http, "GET", URL, service="Example", errors={403: "It's private."})


@respx.mock
async def test_retries_after_server_errors_and_timeouts():
    route = respx.get(URL)
    route.side_effect = [httpx.ConnectTimeout("slow"), httpx.Response(502), httpx.Response(200)]
    async with client() as http:
        response = await request(http, "GET", URL, service="Example", retry_delays=(0, 0))
    assert response.status_code == 200 and route.call_count == 3


@respx.mock
async def test_gives_up_after_the_last_try_with_its_own_message():
    route = respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    async with client() as http:
        with pytest.raises(SourceError, match="Down or very slow") as caught:
            await request(
                http,
                "GET",
                URL,
                service="Example",
                retry_delays=(0, 0),
                unavailable="Down or very slow.",
            )
    assert route.call_count == 3
    assert (
        caught.value.details["error_type"] == "ReadTimeout" and caught.value.details["tries"] == 3
    )


@respx.mock
async def test_network_errors_and_timeouts_without_retries():
    respx.get(URL).mock(side_effect=httpx.ConnectError("no route to host"))
    async with client() as http:
        with pytest.raises(SourceError, match="couldn't reach Example") as caught:
            await request(http, "GET", URL, service="Example")
    assert caught.value.details["error_type"] == "ConnectError"
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    async with client() as http:
        with pytest.raises(SourceError, match="didn't answer in time"):
            await request(http, "GET", URL, service="Example")


@respx.mock
async def test_short_rate_limits_are_waited_out_when_retrying():
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(429, headers={"X-RateLimit-Reset-In": "0"}),
        httpx.Response(200),
    ]
    async with client() as http:
        await request(http, "GET", URL, service="Example", retry_delays=(0,))
    assert route.call_count == 2
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "3600"})]
    async with client() as http:
        with pytest.raises(SourceError, match="limiting requests") as caught:
            await request(http, "GET", URL, service="Example", retry_delays=(0,))
    assert caught.value.details["retry_after"] == 3600


@respx.mock
async def test_answers_that_are_not_json():
    respx.get(URL).respond(200, text="<html>maintenance</html>")
    async with client() as http:
        with pytest.raises(SourceError, match="can't read") as caught:
            await get_json(http, URL, service="Example")
    assert caught.value.details["excerpt"] == "<html>maintenance</html>"


def test_listenbrainz_tries_three_times_within_a_minute():
    assert len(listenbrainz.RETRY_DELAYS) == 2 and sum(listenbrainz.RETRY_DELAYS) < 60


def test_json_out_of_web_pages():
    page = (
        '<script src="/app.js"></script><script type="application/json" id="data">{"a": [1]}'
        "</script><script id='other'>var x = 1;</script>"
    )
    assert script_json(page, "data") == {"a": [1]}
    assert script_json(page, "missing") is None
    assert script_ids(page) == ["data", "other"]
    with pytest.raises(ValueError):
        script_json(page, "other")


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("PT3M27S", 207.0), ("PT1H2M3S", 3723.0), ("PT45.5S", 45.5), ("P1DT1S", 86401.0),
     ("PT", None), ("", None), (None, None), ("3:27", None)],
)  # fmt: skip
def test_iso_durations(value, seconds):
    assert iso_seconds(value) == seconds


def test_small_conversions():
    assert ms_to_seconds(223935) == 223.935
    assert ms_to_seconds(0) is None and ms_to_seconds(None) is None and ms_to_seconds(True) is None
    assert html_to_text("<p>Hits by <b>Olivia</b> &amp; more</p>\n<p>Updated  weekly.</p>") == (
        "Hits by Olivia & more\nUpdated weekly."
    )
    assert html_to_text("") is None and html_to_text(None) is None


@pytest.mark.parametrize(
    ("title", "channel", "artist", "song"),
    [
        ("Daft Punk - Get Lucky (Official Video)", "DaftPunkVEVO", "Daft Punk", "Get Lucky"),
        ("BELLAKEO (Video Oficial) - Peso Pluma, Anitta", "Peso Pluma", "Peso Pluma, Anitta",
         "BELLAKEO"),
        ("Arcángel - FN8 ( Video Lyric )", "Arcangel", "Arcángel", "FN8"),
        ("Get Lucky", "Daft Punk - Topic", "Daft Punk", "Get Lucky"),
        ("Get Lucky - Radio Edit", "Daft Punk", "Daft Punk", "Get Lucky (Radio Edit)"),
        ("La Diabla", "Xavi Oficial", "Xavi", "La Diabla"),
        ("Strobe (Extended Mix) [HD]", "deadmau5", "deadmau5", "Strobe (Extended Mix)"),
        ("Cloonee & Chris Lake - Paradise (ft. Aliyahs Interlude)", "Cloonee",
         "Cloonee & Chris Lake", "Paradise (ft. Aliyahs Interlude)"),
        ("Untitled", None, None, "Untitled"),
    ],
)  # fmt: skip
def test_artist_and_title_from_video_titles(title, channel, artist, song):
    assert split_artist_title(title, channel) == (artist, song)


def test_clean_title_keeps_mix_names_and_live():
    assert clean_title("Song (Live) [Official Audio]") == "Song (Live)"
    assert clean_title("(Official Video)") == "(Official Video)"  # nothing left: keep it
