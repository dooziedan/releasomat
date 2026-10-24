"""The Test button for Soulseek in Settings (``check_connection``): saved answers shaped like slskd
0.26.1's ``GET /api/v0/application``, and the fake slskd. Nothing goes online."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.downloaders.base import DownloaderError
from app.downloaders.slskd import check_connection

ANSWERS = Path(__file__).parent.parent / "fixtures" / "online"
URL = "http://192.168.1.5:5030"
KEY = "owner-key-8b1d0c7f2e9a"


def saved(name: str) -> dict:
    return json.loads((ANSWERS / name).read_text())


async def problem(answer: httpx.Response | Exception) -> DownloaderError:
    """The error ``check_connection`` raises when slskd answers ``answer``."""
    with respx.mock, pytest.raises(DownloaderError) as caught:
        route = respx.get(f"{URL}/api/v0/application")
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        await check_connection(URL, KEY)
    assert KEY not in str(caught.value) + json.dumps(caught.value.details, default=str)
    return caught.value


async def test_connected_and_logged_in():
    with respx.mock:
        route = respx.get(f"{URL}/api/v0/application").mock(
            return_value=httpx.Response(200, json=saved("slskd_application.json"))
        )
        message = await check_connection(URL + "/", KEY)
    assert message == "Connected to slskd 0.26.1, logged in to Soulseek."
    request = route.calls.last.request
    assert request.headers["X-API-Key"] == KEY
    assert request.headers["User-Agent"].startswith("Releasomat/")
    assert KEY not in str(request.url)


async def test_a_pasted_api_path_or_missing_scheme_is_fine():
    with respx.mock:
        respx.get(f"{URL}/api/v0/application").mock(
            return_value=httpx.Response(200, json=saved("slskd_application.json"))
        )
        assert (await check_connection("192.168.1.5:5030/api/v0/", KEY)).startswith("Connected")


async def test_wrong_key():
    error = await problem(httpx.Response(401))
    assert str(error) == "slskd refused the API key (HTTP 401): check it in Settings."


async def test_key_not_allowed_from_this_address():
    error = await problem(httpx.Response(403))
    assert "HTTP 403" in str(error)


async def test_not_logged_in_to_soulseek():
    error = await problem(httpx.Response(200, json=saved("slskd_application_logged_out.json")))
    assert str(error) == (
        "slskd 0.26.1 answers, but isn't logged in to Soulseek (Disconnected). Check its "
        "Soulseek user name and password, or wait until it has reconnected."
    )


async def test_too_old():
    state = saved("slskd_application.json")
    state["version"]["current"] = "0.24.5"
    error = await problem(httpx.Response(200, json=state))
    assert str(error) == (
        "slskd 0.24.5 is too old: Releasomat needs slskd 0.26 or newer. Please update it."
    )


async def test_unreachable():
    error = await problem(httpx.ConnectError("[Errno 111] Connection refused"))
    assert str(error) == (
        "Releasomat can't reach slskd at http://192.168.1.5:5030 ([Errno 111] Connection "
        "refused). Check the address and that slskd is running."
    )
    timeout = await problem(httpx.ReadTimeout("timed out"))
    assert str(timeout) == "slskd at http://192.168.1.5:5030 didn't answer in time."


async def test_something_else_answers():
    error = await problem(httpx.Response(404, text="<html>Not found</html>"))
    assert str(error).startswith("http://192.168.1.5:5030 answers, but not like slskd (HTTP 404)")
    html = await problem(httpx.Response(200, text="<html>Navidrome</html>"))
    assert str(html).startswith("http://192.168.1.5:5030 answers, but not like slskd.")


async def test_nothing_entered():
    for url, key in (("", KEY), (URL, ""), (URL, "   ")):
        with pytest.raises(DownloaderError, match="Add slskd's address and API key first."):
            await check_connection(url, key)


async def test_against_the_fake(make_fake, api_key):
    fake = make_fake()
    transport = httpx.ASGITransport(app=fake.app)
    message = await check_connection("http://slskd.test:5030", api_key, transport=transport)
    assert message == "Connected to slskd 0.26.1, logged in to Soulseek."
    with pytest.raises(DownloaderError, match="refused the API key"):
        await check_connection("http://slskd.test:5030", "wrong-key", transport=transport)
    logged_out = httpx.ASGITransport(app=make_fake(logged_in=False).app)
    with pytest.raises(DownloaderError, match="isn't logged in to Soulseek"):
        await check_connection("http://slskd.test:5030", api_key, transport=logged_out)
