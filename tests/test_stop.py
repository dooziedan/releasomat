"""The Stop button: a running refresh ends at once as "stopped", a waiting one is dropped.

What must hold after a stop: the current download is cancelled (the YouTube worker process is
ended, the Soulseek transfer cancelled in slskd), tracks saved before stay "done" and aren't
downloaded again, nothing half-finished reaches MUSIC_DIR, the Navidrome playlist is not touched,
and the playlist is free again for the next refresh. Tests never go online: the source, Navidrome
and slskd are fakes (slskd's runs in-process).
"""

import asyncio
import shutil
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, select
from test_pipeline import (
    AUDIO,
    EPLE,
    QUEEN,
    STROBE,
    FakeNavidrome,
    FakeSource,
    _add,
    fake_cover,
    music_files,
)

from app import pipeline
from app.downloaders import youtube
from app.downloaders.base import Downloader, DownloadResult
from app.downloaders.slskd import SlskdDownloader
from app.downloaders.youtube import YouTubeDownloader, run_in_fresh_process
from app.events import EventBus
from app.models import Playlist, Run, Track, TrackFile
from app.pipeline import Services
from app.sources.base import PlaylistSnapshot, SourceTrack
from app.worker import DROPPED_MESSAGE, Worker
from scripts.fake_slskd import FakeSlskd, Song

GLUE = SourceTrack("Bicep", "Glue", duration=269)
LINUX = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="looks at Linux's /proc")


class HangingDownloader(Downloader):
    """Saves the test tone at once, except for the ``hang`` titles: those write half a file into
    the staging folder and then wait, like a stuck download, until they are cancelled."""

    name = "youtube"
    label = "YouTube"

    def __init__(self, hang=()):
        self.hang = hang
        self.calls: list[str] = []
        self.hanging: list[str] = []
        self.cancelled: list[str] = []

    async def download(self, track, staging_dir):
        self.calls.append(track.title)
        if track.title in self.hang:
            (staging_dir / "half-finished.opus.part").write_bytes(b"0" * 1000)
            self.hanging.append(track.title)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.append(track.title)
                raise
        target = staging_dir / "abc123.opus"
        shutil.copy(AUDIO / "tone.opus", target)
        return DownloadResult(
            path=target, format="opus", bitrate=7, downloader="youtube", origin="https://y/1"
        )


class SlowNavidrome(FakeNavidrome):
    """A fake Navidrome whose scan or playlist saving waits until ``go`` is set."""

    def __init__(self, music_dir, wait_in: str):
        super().__init__(music_dir)
        self.wait_in = wait_in
        self.waiting = asyncio.Event()
        self.go = asyncio.Event()

    async def _slow(self, name):
        if name == self.wait_in:
            self.waiting.set()
            await self.go.wait()

    async def scan(self):
        await self._slow("scan")
        return await super().scan()

    async def create_playlist(self, name, song_ids):
        await self._slow("create_playlist")
        return await super().create_playlist(name, song_ids)


async def until(condition, timeout: float = 5.0) -> None:
    """Wait until ``condition()`` is true."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture
async def world(engine, config, monkeypatch):
    """A playlist with one song in the library and three to download, a fake source and
    Navidrome, and workers that refresh with them (one download at a time)."""

    class World:
        def __init__(self):
            self.source = FakeSource(PlaylistSnapshot("My Mix", [QUEEN, EPLE, STROBE, GLUE]))
            self.navidrome = FakeNavidrome(config.music_dir)
            self.events = EventBus()
            self.seen: list = []  # every event published
            self.workers: list[Worker] = []

        def services(self, downloaders) -> Services:
            return Services(
                navidrome=self.navidrome,
                downloaders=downloaders,
                config=config,
                slots=asyncio.Semaphore(1),
                cover=fake_cover,
            )

        def worker(self, *downloaders) -> Worker:
            async def refresh(playlist_id, trigger, stop):
                await pipeline.refresh_playlist(
                    playlist_id,
                    trigger,
                    engine=engine,
                    events=self.events,
                    services=self.services(list(downloaders)),
                    stop=stop,
                )

            worker = Worker(engine, refresh=refresh, events=self.events)
            worker.start()
            self.workers.append(worker)
            return worker

        def runs(self) -> list[Run]:
            with Session(engine) as session:
                return list(session.exec(select(Run).order_by(Run.id)).all())

        def playlist(self) -> Playlist:
            with Session(engine) as session:
                return session.get(Playlist, self.playlist_id)

        def files(self) -> dict[str, TrackFile]:
            with Session(engine) as session:
                rows = session.exec(select(TrackFile, Track).join(Track)).all()
                return {track.title: file for file, track in rows}

        def run_events(self) -> list[dict]:
            return [e.data for e in self.seen if e.type == "run"]

    world = World()
    world.playlist_id = _add(engine)
    monkeypatch.setattr(pipeline, "get_source", lambda source: world.source)

    async def listen():
        async for event in world.events.listen():
            world.seen.append(event)

    listener = asyncio.create_task(listen())
    yield world
    for worker in world.workers:
        await worker.stop()
    listener.cancel()


def staging_folders(config) -> list[Path]:
    root = config.config_dir / "staging"
    return list(root.iterdir()) if root.exists() else []


async def test_stop_during_a_download_keeps_what_was_saved(world, config):
    downloader = HangingDownloader(hang=("Strobe (Extended Mix)",))
    worker = world.worker(downloader)
    assert worker.request_refresh(world.playlist_id, "manual")
    await until(lambda: downloader.hanging)  # Eple is saved, Strobe hangs, Glue waits its turn
    assert worker.state(world.playlist_id) == "running"

    result = await worker.stop_refresh(world.playlist_id)

    message = (
        "Stopped by you after 1 of 3 tracks to download (1 downloaded). The Navidrome playlist "
        "was left as it is. The downloaded songs stay and are added with the next refresh."
    )
    assert (result.outcome, result.message) == ("stopped", message)
    (run,) = world.runs()
    assert (run.id, run.status, run.message) == (result.run_id, "stopped", message)
    assert run.finished_at is not None and run.log.rstrip().endswith(message)
    assert run.report["step"] == "download" and run.report["message"] == message
    assert run.report["details"]["cut_off_downloads"] == (
        "deadmau5 – Strobe (Extended Mix) (YouTube)"
    )
    assert world.playlist().last_status == "stopped"

    # The current download was cancelled; the one still waiting for a slot never started.
    assert downloader.calls == ["Eple", "Strobe (Extended Mix)"]
    assert downloader.cancelled == ["Strobe (Extended Mix)"]
    # Eple stays saved and done; the half-finished Strobe never reached MUSIC_DIR.
    assert music_files(config) == ["Röyksopp/Röyksopp - Eple.opus"]
    assert staging_folders(config) == []
    files = world.files()
    eple, strobe = files["Eple"], files["Strobe (Extended Mix)"]
    assert (eple.status, eple.downloaded_by_releasomat) == ("done", True)
    assert eple.path == "Röyksopp/Röyksopp - Eple.opus"
    assert (strobe.status, strobe.path, strobe.error) == ("pending", None, pipeline.CUT_OFF)
    assert "Glue" not in files
    # Navidrome: no scan, and the playlist was neither created nor changed.
    assert world.navidrome.calls == ["find_in_library"] * 4
    assert world.navidrome.playlists == {}

    # Live update for open pages.
    last = world.run_events()[-1]
    assert (last["run_id"], last["status"], last["message"]) == (run.id, "stopped", message)

    # The playlist is free again at once, and the next refresh only downloads what is missing.
    assert worker.state(world.playlist_id) is None
    downloader.hang = ()
    assert worker.request_refresh(world.playlist_id, "manual") is True
    await asyncio.wait_for(worker.join(), 5)
    assert downloader.calls[2:] == ["Strobe (Extended Mix)", "Glue"]  # not Eple again
    run = world.runs()[-1]
    assert run.status == "done" and "3 downloaded (2 new)" in run.message
    (songs,) = world.navidrome.playlists.values()
    assert len(songs["songs"]) == 4


async def test_after_a_stop_no_track_stays_marked_as_downloading(world, config):
    """Soulseek first (finds nothing), YouTube hangs on Strobe: after Stop no track may stay
    "downloading", or the next refresh would wait for a download that no longer runs."""
    from test_pipeline import FakeSoulseek

    titles = ["Eple", "Strobe (Extended Mix)", "Glue"]
    youtube = HangingDownloader(hang=("Strobe (Extended Mix)",))
    worker = world.worker(FakeSoulseek(fail=titles), youtube)
    assert worker.request_refresh(world.playlist_id, "manual")
    await until(lambda: youtube.hanging)
    assert (await worker.stop_refresh(world.playlist_id)).outcome == "stopped"
    assert [f.status for f in world.files().values()].count("downloading") == 0
    assert pipeline.DOWNLOADING_NOW == {}

    youtube.hang = ()
    assert worker.request_refresh(world.playlist_id, "manual") is True
    await asyncio.wait_for(worker.join(), 5)  # doesn't wait for a download that isn't there
    run = world.runs()[-1]
    assert run.status == "done", run.message


async def test_stop_during_the_navidrome_scan(world, config):
    world.navidrome = SlowNavidrome(config.music_dir, wait_in="scan")
    worker = world.worker(HangingDownloader())
    worker.request_refresh(world.playlist_id, "manual")
    await asyncio.wait_for(world.navidrome.waiting.wait(), 5)

    result = await worker.stop_refresh(world.playlist_id)

    assert result.outcome == "stopped"
    assert result.message.startswith(
        "Stopped by you while Navidrome was scanning (3 downloaded). The Navidrome playlist was "
        "left as it is."
    )
    (run,) = world.runs()
    assert run.status == "stopped" and run.report["step"] == "scan"
    assert "create_playlist" not in world.navidrome.calls and world.navidrome.playlists == {}
    assert {f.status for f in world.files().values()} == {"in_library", "done"}
    assert len(music_files(config)) == 3

    world.navidrome.go.set()  # Navidrome is quick again: the next refresh links the downloads
    worker.request_refresh(world.playlist_id, "manual")
    await asyncio.wait_for(worker.join(), 5)
    run = world.runs()[-1]
    assert run.status == "done" and "Navidrome playlist created (4 songs)." in run.message


async def test_too_late_while_the_navidrome_playlist_is_saved(world, config):
    """Navidrome never gets half a playlist: once it is being saved, the refresh finishes."""
    world.navidrome = SlowNavidrome(config.music_dir, wait_in="create_playlist")
    worker = world.worker(HangingDownloader())
    worker.request_refresh(world.playlist_id, "manual")
    await asyncio.wait_for(world.navidrome.waiting.wait(), 5)

    result = await worker.stop_refresh(world.playlist_id, wait=0.2)

    assert result.outcome == "too_late"
    assert result.message.startswith("Too late to stop: the refresh is saving the Navidrome")
    assert worker.state(world.playlist_id) == "running"
    world.navidrome.go.set()
    await asyncio.wait_for(worker.join(), 5)
    (run,) = world.runs()
    assert run.status == "done" and "Navidrome playlist created (4 songs)." in run.message


async def test_stop_while_reading_the_playlist(world):
    class SlowSource:
        async def fetch(self, ref):
            await asyncio.sleep(60)

    world.source = SlowSource()
    worker = world.worker(HangingDownloader())
    worker.request_refresh(world.playlist_id, "manual")
    await until(lambda: world.runs())
    result = await worker.stop_refresh(world.playlist_id)
    assert result.outcome == "stopped"
    assert result.message == (
        "Stopped by you while reading the playlist from Deezer. The Navidrome playlist was left "
        "as it is."
    )
    assert world.navidrome.calls == [] and world.files() == {}


async def test_a_waiting_refresh_is_dropped(world, engine):
    other_id = _add(engine, source_id="1111")
    started, release = [], asyncio.Event()

    async def slow_refresh(playlist_id, trigger, stop):
        started.append(playlist_id)
        await release.wait()

    worker = Worker(engine, refresh=slow_refresh, workers=1, events=world.events)
    world.workers.append(worker)
    worker.start()
    worker.request_refresh(world.playlist_id, "manual")
    await until(lambda: started)
    worker.request_refresh(other_id, "manual")  # waits: the only worker is busy
    assert worker.state(other_id) == "waiting"

    result = await worker.stop_refresh(other_id)

    assert (result.outcome, result.run_id) == ("dropped", None)
    assert worker.state(other_id) is None
    await asyncio.sleep(0.01)
    assert world.run_events()[-1] == {
        "run_id": None,
        "playlist_id": other_id,
        "status": "stopped",
        "message": DROPPED_MESSAGE,
    }
    release.set()
    await asyncio.wait_for(worker.join(), 5)
    assert started == [world.playlist_id]  # the dropped one never started
    assert worker.request_refresh(other_id, "manual") is True  # and can be asked for again
    await asyncio.wait_for(worker.join(), 5)
    assert started == [world.playlist_id, other_id]


async def test_stopping_when_nothing_runs(world):
    worker = world.worker(HangingDownloader())
    result = await worker.stop_refresh(world.playlist_id)
    assert result.outcome == "not_running"
    assert result.message == "Nothing to stop: this playlist isn't refreshing."
    assert world.runs() == []


@LINUX
async def test_stop_ends_the_youtube_worker_process(world, config, tmp_path, monkeypatch):
    """With the real YouTube downloader: its worker process (and what it started) is ended,
    and the half-finished file in the staging folder is thrown away."""
    program, pids = fake_job_program(tmp_path)
    monkeypatch.setattr(sys, "executable", str(program))
    world.source = FakeSource(PlaylistSnapshot("My Mix", [EPLE]))
    worker = world.worker(YouTubeDownloader(config_dir=config.config_dir, pause=0))
    worker.request_refresh(world.playlist_id, "manual")
    await until(lambda: pids.exists() and pids.read_text())
    job, child = map(int, pids.read_text().split())
    assert running(job) and running(child)
    assert [p.name for p in staging_folders(config)[0].iterdir()] == ["half-finished.opus.part"]

    result = await worker.stop_refresh(world.playlist_id)

    assert result.outcome == "stopped"
    await until(lambda: not running(job) and not running(child))
    assert staging_folders(config) == [] and music_files(config) == []
    assert world.files()["Eple"].status == "pending"
    assert world.runs()[0].report["details"]["cut_off_downloads"] == "Röyksopp – Eple (YouTube)"


async def test_stop_cancels_the_soulseek_transfer(world, config, tmp_path):
    """With the real Soulseek downloader and the fake slskd: the transfer that is running is
    cancelled and taken off slskd's list; nothing lands in MUSIC_DIR or slskd's folder."""
    world.source = FakeSource(PlaylistSnapshot("My Mix", [EPLE]))
    downloads = tmp_path / "slskd"
    downloads.mkdir()
    song = Song("Röyksopp", "Eple", "Melody A.M.", 219, 2001, 2)
    # "stall": the best peer's transfer stops at 30 % and waits there.
    fake = FakeSlskd([song], downloads, "test-key-5f2c9a7e41b3", speed=50, scenario="stall",
                     make_audio=lambda target, ext, seconds: None)  # fmt: skip
    soulseek = SlskdDownloader(
        "http://slskd.test:5030",
        "test-key-5f2c9a7e41b3",
        downloads_dir=downloads,
        transport=httpx.ASGITransport(app=fake.app),
        poll=0.01,
        stall_limit=60,
    )
    worker = world.worker(soulseek)
    try:
        worker.request_refresh(world.playlist_id, "manual")
        await until(lambda: any(t.state == "InProgress" for t in fake.transfers.values()))

        result = await worker.stop_refresh(world.playlist_id)

        assert result.outcome == "stopped"
        (transfer,) = fake.transfers.values()
        assert (transfer.state, transfer.removed) == ("Completed, Cancelled", True)
        assert fake.searches == {}  # the search was deleted, too
        assert [p for p in downloads.rglob("*") if p.is_file()] == []
        assert music_files(config) == [] and staging_folders(config) == []
        assert world.files()["Eple"].status == "pending"
        cut_off = world.runs()[0].report["details"]["cut_off_downloads"]
        assert cut_off == "Röyksopp – Eple (Soulseek (slskd))"
    finally:
        await fake.close()


# --- The worker process of a YouTube download ---------------------------------------------------


def fake_job_program(tmp_path: Path, *, ignore_stop: bool = False) -> tuple[Path, Path]:
    """A stand-in for ``python -m app.downloaders.youtube_job``: it writes half a download into
    the staging folder, starts a child process (as yt-dlp starts ffmpeg), notes both process IDs
    in ``pids`` and then hangs like a stuck download. With ``ignore_stop`` it ignores SIGTERM."""
    pids = tmp_path / "pids"
    program = tmp_path / "fake-job"
    ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_stop else ""
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"{ignore}\n"
        "job = json.load(sys.stdin)\n"
        "Path(job['staging_dir'], 'half-finished.opus.part').write_bytes(b'0' * 1000)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(60)\n"
    )
    program.chmod(0o755)
    return program, pids


def running(pid: int) -> bool:
    """Whether a process still runs (one that has ended but isn't cleaned up yet doesn't)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


@LINUX
async def test_a_worker_process_that_ignores_the_stop_is_killed(tmp_path, monkeypatch):
    program, pids = fake_job_program(tmp_path, ignore_stop=True)
    monkeypatch.setattr(sys, "executable", str(program))
    monkeypatch.setattr(youtube, "END_GRACE", 0.3)
    staging = tmp_path / "staging"
    staging.mkdir()
    task = asyncio.create_task(run_in_fresh_process({"staging_dir": str(staging)}))
    await until(lambda: pids.exists() and pids.read_text())
    job, child = map(int, pids.read_text().split())

    started = time.monotonic()
    task.cancel()  # what the Stop button does to the download
    with pytest.raises(asyncio.CancelledError):
        await task

    assert 0.3 <= time.monotonic() - started < 3  # asked first, killed after the grace time
    await until(lambda: not running(job) and not running(child))


# --- Through the web app: the JSON API, the forms and the pages ---------------------------------


class SlowSource:
    """A source that takes a minute to answer: the refresh stays in its first step."""

    async def fetch(self, ref):
        await asyncio.sleep(60)


def latest_run(client, playlist_id: int, status: str) -> dict:
    """The playlist's newest run, once it has ``status`` (the worker runs in the app's loop)."""
    for _ in range(250):
        runs = client.get("/api/runs", params={"playlist_id": playlist_id}).json()
        if runs and runs[0]["status"] == status:
            return runs[0]
        time.sleep(0.02)
    raise AssertionError(f"no {status} run")


def test_stop_through_the_api_and_the_forms(client, monkeypatch):
    monkeypatch.setattr(pipeline, "get_source", lambda source: SlowSource())
    url = "https://www.deezer.com/playlist/908622995"
    playlist_id = client.post("/api/playlists", json={"url": url}).json()["id"]
    stop_form = f'action="/playlists/{playlist_id}/stop"'

    # Nothing runs yet: a clear answer, never an error page.
    response = client.post(f"/api/playlists/{playlist_id}/stop")
    assert response.status_code == 409 and response.json()["outcome"] == "not_running"
    response = client.post(f"/playlists/{playlist_id}/stop", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Refresh now" in response.text and "Nothing to stop" in response.text

    client.post(f"/api/playlists/{playlist_id}/refresh")
    run = latest_run(client, playlist_id, "running")
    assert stop_form in client.get(f"/playlists/{playlist_id}").text
    response = client.post(f"/api/playlists/{playlist_id}/stop")
    assert response.status_code == 200
    assert response.json() == {
        "outcome": "stopped",
        "message": "Stopped by you while reading the playlist from Deezer. The Navidrome "
        "playlist was left as it is.",
        "run_id": run["id"],
    }
    run = client.get(f"/api/runs/{run['id']}").json()
    assert run["status"] == "stopped" and run["finished_at"]
    page = client.get(f"/playlists/{playlist_id}").text
    assert '<span class="badge stopped">Stopped</span>' in page
    assert "Refresh now" in page and stop_form not in page

    # Free again at once. This time it is stopped from the Activity page (htmx form).
    assert client.post(f"/api/playlists/{playlist_id}/refresh").status_code == 202
    run = latest_run(client, playlist_id, "running")
    run_form = f'action="/runs/{run["id"]}/stop"'
    assert run_form in client.get("/activity").text
    assert run_form not in client.get(f"/playlists/{playlist_id}").text  # only on Activity
    response = client.post(f"/runs/{run['id']}/stop", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert '<span class="badge stopped">Stopped</span>' in response.text
    assert run_form not in response.text
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "stopped"

    response = client.post(f"/api/runs/{run['id']}/stop")  # once more: nothing to stop
    assert response.status_code == 409 and response.json()["outcome"] == "not_running"
    response = client.post(
        f"/playlists/{playlist_id}/stop",
        headers={"referer": f"http://testserver/playlists/{playlist_id}"},
        follow_redirects=False,
    )  # the plain form, without htmx: back to the page
    assert response.status_code == 303
    assert response.headers["location"] == f"http://testserver/playlists/{playlist_id}"
    assert client.post("/api/runs/999/stop").status_code == 404
    assert client.post("/api/playlists/999/stop").status_code == 404


def test_the_stop_button_shows_only_while_a_refresh_waits_or_runs(client, monkeypatch):
    url = "https://www.deezer.com/playlist/908622995"
    playlist_id = client.post("/api/playlists", json={"url": url}).json()["id"]
    worker = client.app.state.worker
    stop_form = f'action="/playlists/{playlist_id}/stop"'
    pages = ("/", f"/playlists/{playlist_id}", "/partials/playlists")
    for state, shown in ((None, False), ("waiting", True), ("running", True), ("stopping", False)):
        monkeypatch.setattr(worker, "state", lambda playlist_id, state=state: state)
        for page in pages:
            assert (stop_form in client.get(page).text) is shown, (state, page)
    assert "Stopping…" in client.get(f"/playlists/{playlist_id}").text
