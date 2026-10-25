"""Soulseek downloads against the fake slskd, one test per scenario: success, nothing found, only
low quality, peer offline, rejected, stalled, wrong file, slow search, exact search finding
nothing. The fake runs in-process (see conftest.py): nothing goes online."""

import asyncio
import json

import httpx
import pytest
import respx

from app.downloaders import slskd
from app.downloaders.base import DownloaderError, DownloaderNotReady
from app.downloaders.slskd import NOT_CONNECTED, SlskdDownloader
from app.files import SLSKD_FOLDER
from app.sources.base import SourceTrack
from app.tagging import audio_info

GET_LUCKY = SourceTrack("Daft Punk", "Get Lucky", "Random Access Memories", 4)


def scenario(word: str) -> SourceTrack:
    return SourceTrack("Releasomat Test", word, "Scenario Songs", 4)


def own_folder(fake):
    return fake.downloads_dir / SLSKD_FOLDER


def files_in(folder) -> list:
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


async def test_a_download_lands_in_the_staging_folder(fake, make_downloader, staging):
    result = await make_downloader(fake).download(GET_LUCKY, staging)
    assert result.downloader == "slskd" and result.format == "flac"
    assert result.origin == "Soulseek: flacfan · 08 - Get Lucky.flac"
    assert result.path.parent == staging and result.path.name == "08 - Get Lucky.flac"
    assert audio_info(result.path).duration == pytest.approx(2, abs=0.2)
    assert result.duration == pytest.approx(2, abs=0.2)
    # Moved, not copied: slskd's folder keeps nothing of it, but Releasomat's folder stays.
    assert files_in(fake.downloads_dir) == []
    assert own_folder(fake).is_dir() and list(own_folder(fake).iterdir()) == []
    # It went into its own batch folder, and slskd's lists were tidied up.
    (transfer,) = fake.transfers.values()
    assert transfer.destination.startswith(f"{SLSKD_FOLDER}/") and transfer.removed
    assert transfer.local_filename.endswith(f"{transfer.destination}/08 - Get Lucky.flac")
    assert fake.searches == {} and fake.search_texts == ["daft punk get lucky"]
    # The rejected files are listed: the 24-bit FLAC peer has no free slot, and so on.
    reasons = [entry["reason"] for entry in result.tried]
    assert "the peer has no free upload slot (queue: 14)" in reasons
    assert "128 kbit/s, at least 320 wanted" in reasons


async def test_accents_are_searched_as_written(fake, make_downloader, staging):
    result = await make_downloader(fake).download(SourceTrack("Beyoncé", "Halo"), staging)
    assert result.origin == "Soulseek: flacfan · 02 - Halo.flac"
    assert fake.search_texts == ["beyoncé halo"]


async def test_mp3_when_the_formats_say_so(fake, make_downloader, staging):
    result = await make_downloader(fake, formats=["mp3"]).download(GET_LUCKY, staging)
    assert result.format == "mp3" and result.bitrate == 320
    assert result.origin.startswith("Soulseek: mp3lover · ")


async def test_nobody_has_it(fake, make_downloader, staging):
    with pytest.raises(DownloaderError) as caught:
        await make_downloader(fake).download(scenario("noresults"), staging)
    assert str(caught.value) == (
        "Nobody shares this track. Searched for “releasomat test noresults” and "
        "“*eleasomat test noresults” (the * also finds artists that Soulseek's search filters "
        "out)."
    )
    assert fake.search_texts == ["releasomat test noresults", "*eleasomat test noresults"]
    assert caught.value.details["queries"] == fake.search_texts
    assert fake.transfers == {}


async def test_only_low_quality(fake, make_downloader, staging):
    with pytest.raises(DownloaderError) as caught:
        await make_downloader(fake).download(scenario("lowquality"), staging)
    message = str(caught.value)
    assert message.startswith("Soulseek has 3 files for “releasomat test lowquality”, but none")
    assert "2 × too low bitrate" in message and "1 × other formats" in message
    reasons = [entry["reason"] for entry in caught.value.tried]
    assert reasons == [
        "190 kbit/s, at least 320 wanted",
        "128 kbit/s, at least 320 wanted",
        "M4A isn't wanted (FLAC, AIFF, WAV, MP3)",
    ]
    assert fake.transfers == {} and len(fake.search_texts) == 1  # files were found: no 2nd search


async def test_an_offline_peer_means_the_next_one(fake, make_downloader, staging):
    result = await make_downloader(fake).download(scenario("offline"), staging)
    assert result.origin.startswith("Soulseek: mp3lover · ")
    first = result.tried[0]
    assert first["candidate"].startswith("Soulseek: flacfan · ")
    assert first["reason"] == 'the peer is offline ("User flacfan appears to be offline")'


async def test_a_rejected_transfer_means_the_next_peer(fake, make_downloader, staging):
    result = await make_downloader(fake).download(scenario("rejected"), staging)
    assert result.origin.startswith("Soulseek: mp3lover · ")
    assert result.tried[0]["reason"] == "the peer refused the download (File not shared.)"


async def test_a_stalled_transfer_is_cancelled_and_the_next_peer_tried(
    fake, make_downloader, staging
):
    result = await make_downloader(fake).download(scenario("stall"), staging)
    assert result.origin.startswith("Soulseek: mp3lover · ")
    assert result.tried[0]["reason"].startswith("the transfer stalled: no progress for 0 seconds")
    stalled = next(t for t in fake.transfers.values() if t.username == "flacfan")
    assert stalled.state == "Completed, Cancelled" and stalled.removed


async def test_a_wrong_file_stays_in_slskds_folder_and_the_next_peer_is_tried(
    fake, make_downloader, staging
):
    result = await make_downloader(fake).download(scenario("wrongfile"), staging)
    assert result.origin.startswith("Soulseek: mp3lover · ")
    reason = result.tried[0]["reason"]
    assert reason.startswith("the file is 0:40 long instead of 0:04; it stays in slskd's folder")
    # Releasomat never deletes on its own: the wrong file is still there for the owner.
    (left,) = files_in(own_folder(fake))
    assert left.name == "06 - wrongfile.flac" and left.parent.name in reason


async def test_a_slow_search_is_stopped_and_its_answers_used(make_fake, make_downloader, staging):
    fake = make_fake(speed=20)  # the slow search takes 1.5 s; answers arrive one by one
    downloader = make_downloader(fake, search_time=0.3)
    result = await downloader.download(scenario("slowsearch"), staging)
    assert result.origin.startswith("Soulseek: flacfan · ")
    (search,) = fake.history
    assert search.state() == "Completed, Cancelled"
    assert 0 < len(search.arrived()) < len(search.arrivals)  # stopped before all had answered


async def test_when_the_exact_search_finds_nothing_a_simpler_one_runs(
    fake, make_downloader, staging
):
    result = await make_downloader(fake).download(scenario("exactfails"), staging)
    assert result.origin.startswith("Soulseek: flacfan · ")
    assert fake.search_texts == ["releasomat test exactfails", "*eleasomat test exactfails"]


async def test_three_failed_peers_give_up_with_every_reason(make_fake, make_downloader, staging):
    fake = make_fake(failing_peers={"flacfan", "mp3lover", "vinyl_vault", "lofi_larry"})
    with pytest.raises(DownloaderError) as caught:
        await make_downloader(fake, min_bitrate=128).download(scenario("rejected"), staging)
    assert str(caught.value) == (
        "3 Soulseek downloads tried, none worked: the peer refused the download (File not shared.)"
    )
    tries = caught.value.tried[:3]
    assert [t["candidate"].split(" · ")[0] for t in tries] == [
        "Soulseek: flacfan",
        "Soulseek: mp3lover",
        "Soulseek: lofi_larry",
    ]
    assert len(fake.transfers) == 3


async def test_searches_run_one_at_a_time(fake, make_downloader, tmp_path):
    from app import files

    downloader = make_downloader(fake)
    tracks = [GET_LUCKY, SourceTrack("Beyoncé", "Halo"), SourceTrack("Queen", "Bohemian Rhapsody")]
    stagings = [files.new_staging_dir(tmp_path / "config") for _ in tracks]
    results = await asyncio.gather(
        *(downloader.download(t, s) for t, s in zip(tracks, stagings, strict=True))
    )
    assert len(results) == 3 and fake.overlaps == 0
    assert len({r.path for r in results}) == 3


async def test_a_busy_slskd_is_asked_again(fake, make_downloader, staging, monkeypatch):
    monkeypatch.setattr(slskd, "BUSY_PAUSE", 0.01)
    inner = httpx.ASGITransport(app=fake.app)
    answered = []

    class BusyOnce(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.method == "POST" and not answered:
                answered.append(request.url.path)
                return httpx.Response(429, json="Only one concurrent operation is permitted.")
            return await inner.handle_async_request(request)

    result = await make_downloader(fake, transport=BusyOnce()).download(GET_LUCKY, staging)
    assert answered == ["/api/v0/searches"] and result.format == "flac"


async def test_not_connected_is_skipped(staging):
    for downloader in (SlskdDownloader(), SlskdDownloader("http://slskd:5030", "")):
        assert not downloader.configured
        with pytest.raises(DownloaderNotReady) as caught:
            await downloader.download(GET_LUCKY, staging)
        assert str(caught.value) == NOT_CONNECTED
    assert NOT_CONNECTED == "Soulseek isn't connected: add its address and API key in Settings."
    assert SlskdDownloader("http://slskd:5030", "key").configured


async def test_not_logged_in_to_soulseek(make_fake, make_downloader, staging):
    fake = make_fake(logged_in=False)
    with pytest.raises(DownloaderError, match="isn't logged in to Soulseek"):
        await make_downloader(fake).download(GET_LUCKY, staging)


async def test_slskds_folder_must_be_mounted(fake, make_downloader, staging, tmp_path):
    downloader = make_downloader(fake, downloads_dir=tmp_path / "not-mounted")
    with pytest.raises(DownloaderError, match=r"slskd's download folder isn't there"):
        await downloader.download(GET_LUCKY, staging)
    assert fake.history == []  # nothing searched or downloaded for nothing


async def test_a_download_that_lands_elsewhere_is_reported(
    fake, make_downloader, staging, tmp_path
):
    elsewhere = tmp_path / "wrong-mount"
    elsewhere.mkdir()
    downloader = make_downloader(fake, downloads_dir=elsewhere, file_wait=0.1)
    with pytest.raises(DownloaderError) as caught:
        await downloader.download(GET_LUCKY, staging)
    assert "slskd finished the download, but Releasomat doesn't find it in" in str(caught.value)
    assert len(fake.transfers) == 1  # no point in trying more peers
    assert caught.value.tried[0]["candidate"].startswith("Soulseek: flacfan · ")


async def test_the_api_key_never_shows(make_fake, make_downloader, staging, api_key):
    def text_of(exc: DownloaderError) -> str:
        return str(exc) + json.dumps(exc.details, default=str) + json.dumps(exc.tried)

    fake = make_fake()
    wrong = "wrong-key-0000000000"
    with pytest.raises(DownloaderError) as caught:
        await make_downloader(fake, key=wrong).download(GET_LUCKY, staging)
    assert str(caught.value) == "slskd refused the API key (HTTP 401): check it in Settings."
    assert wrong not in text_of(caught.value) and api_key not in text_of(caught.value)
    for word in ("noresults", "lowquality"):
        with pytest.raises(DownloaderError) as caught:
            await make_downloader(fake).download(scenario(word), staging)
        assert api_key not in text_of(caught.value)
    unreachable = SlskdDownloader(
        "http://slskd.test:5030", api_key, downloads_dir=fake.downloads_dir
    )
    with respx.mock, pytest.raises(DownloaderError) as caught:
        respx.route(host="slskd.test").mock(
            side_effect=httpx.ConnectError("[Errno 111] Connection refused")
        )
        await unreachable.download(GET_LUCKY, staging)
    assert str(caught.value) == (
        "Releasomat can't reach slskd at http://slskd.test:5030 ([Errno 111] Connection "
        "refused). Check the address and that slskd is running."
    )
    assert api_key not in text_of(caught.value)
    assert api_key not in repr(make_downloader(fake))


async def test_a_file_slskd_wont_queue_means_the_next_peer(fake, make_downloader, staging):
    inner = httpx.ASGITransport(app=fake.app)
    refused = []

    class RefuseFirst(httpx.AsyncBaseTransport):
        """slskd answers 200 when it queued none of the files (here: already in progress)."""

        async def handle_async_request(self, request):
            if request.url.path.endswith("/batches") and not refused:
                filename = json.loads(request.content)["files"][0]["filename"]
                refused.append(filename)
                failures = [{"filename": filename, "message": "Already in progress"}]
                return httpx.Response(200, json={"batch": {"transfers": []}, "failures": failures})
            return await inner.handle_async_request(request)

    result = await make_downloader(fake, transport=RefuseFirst()).download(GET_LUCKY, staging)
    assert result.origin.startswith("Soulseek: mp3lover · ")
    assert result.tried[0]["reason"] == "slskd couldn't queue the file (Already in progress)"


async def test_a_transfer_that_takes_too_long_is_given_up(fake, make_downloader, staging):
    downloader = make_downloader(fake, stall_limit=60, max_time=0.4)
    result = await downloader.download(scenario("stall"), staging)
    assert result.origin.startswith("Soulseek: mp3lover · ")
    assert result.tried[0]["reason"] == "the transfer took longer than 0 seconds"


async def test_a_stop_while_slskd_answers_the_search_start_still_ends_the_search(
    fake, make_downloader, staging
):
    """slskd has made the search, but its answer to the start hasn't arrived when Stop comes:
    the search must not keep running in slskd (it did before; CI's slower machine caught it)."""
    inner = httpx.ASGITransport(app=fake.app)
    answer_held = asyncio.Event()

    class SlowStartAnswer(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await inner.handle_async_request(request)
            if request.method == "POST" and request.url.path.endswith("/searches"):
                answer_held.set()
                await asyncio.Event().wait()  # the answer never arrives before the stop
            return response

    downloader = make_downloader(fake, transport=SlowStartAnswer())
    task = asyncio.create_task(downloader.download(GET_LUCKY, staging))
    await asyncio.wait_for(answer_held.wait(), 5)
    (search,) = fake.searches.values()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert search.state() == "Completed, Cancelled" and fake.searches == {}


async def test_a_stopped_download_stops_the_search_in_slskd(make_fake, make_downloader, staging):
    """The Stop button cancels the download: slskd's search is stopped and deleted at once."""
    fake = make_fake(speed=1)  # the slow search takes 30 s
    task = asyncio.create_task(make_downloader(fake).download(scenario("slowsearch"), staging))
    while not fake.searches:
        await asyncio.sleep(0.01)
    (search,) = fake.searches.values()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert search.state() == "Completed, Cancelled" and fake.searches == {}
    assert fake.transfers == {}
