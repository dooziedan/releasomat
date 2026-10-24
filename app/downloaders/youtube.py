"""YouTube downloader: ytmusicapi finds the song, yt-dlp downloads it (both as Python libraries).

Each download runs in a **fresh worker process** (``run_in_fresh_process``): a new Python that
runs ``python -m app.downloaders.youtube_job``, gets the job as JSON on its input and answers
with JSON. A new process imports yt-dlp anew, so an updated yt-dlp takes effect with the next
download, without restarting Releasomat (ADR 0007). The process ends when the download is done;
if a download hangs, it is stopped after ``JOB_TIMEOUT``.

yt-dlp needs a JavaScript runtime for YouTube (Deno, installed with ``yt-dlp[default,deno]``) and
uses ``CONFIG_DIR/cookies.txt`` when that file exists.

When the refresh is stopped (Stop button) or the download takes too long, the worker process is
ended at once: asked to stop, and killed if it hasn't after ``END_GRACE`` seconds. The pipeline
then throws away whatever it left in the staging folder.
"""

import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path

from app.downloaders.base import Downloader, DownloaderError, DownloadResult
from app.sources.base import SourceTrack

log = logging.getLogger(__name__)

JOB_TIMEOUT = 15 * 60  # seconds one track may take (search, up to 3 downloads, checks)
PAUSE = 5.0  # seconds between two downloads in the same slot (YouTube's wiki suggests 5–10)
END_GRACE = 3.0  # seconds a worker process gets to end after being asked to, before it's killed
JOB_MODULE = "app.downloaders.youtube_job"

Runner = Callable[[dict], Awaitable[dict]]


class WorkerFailed(Exception):
    """The worker process ended without an answer (it crashed or was stopped)."""


async def run_in_fresh_process(job: dict) -> dict:
    """Run one download job in a new Python process and return its answer. The process is
    ended if the caller gives up (Stop button, timeout, Releasomat stopping)."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        JOB_MODULE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Its own process group, so ending it also ends what yt-dlp started (ffmpeg, Deno).
        start_new_session=True,
    )
    try:
        out, err = await process.communicate(json.dumps(job).encode("utf-8"))
    except BaseException:  # stopped or timed out: don't leave the process running
        await end_process(process, END_GRACE)
        raise
    try:
        return json.loads(out.decode("utf-8"))
    except ValueError:
        tail = err.decode("utf-8", "replace").strip()[-1500:]
        log.warning("The download process ended without an answer: %s", tail)
        raise WorkerFailed(tail) from None


async def end_process(process: asyncio.subprocess.Process, grace: float) -> None:
    """End a worker process and everything it started: first ask (SIGTERM), then, if it is
    still there after ``grace`` seconds, kill it (SIGKILL)."""
    if process.returncode is not None:
        return
    _signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), grace)
    except TimeoutError:
        log.info("The download process didn't end within %s s; killing it", grace)
        _signal_group(process, signal.SIGKILL)
        await process.wait()
    except asyncio.CancelledError:  # cancelled again meanwhile (Releasomat stopping): no waiting
        _signal_group(process, signal.SIGKILL)
        raise


def _signal_group(process: asyncio.subprocess.Process, number: int) -> None:
    """Send a signal to the process and its children (its own process group)."""
    with suppress(ProcessLookupError):  # already gone
        os.killpg(process.pid, number)


class YouTubeDownloader(Downloader):
    name = "youtube"
    label = "YouTube"

    def __init__(
        self,
        *,
        config_dir: Path,
        threshold: int = 85,
        pause: float = PAUSE,
        runner: Runner = run_in_fresh_process,
        timeout: float = JOB_TIMEOUT,
    ):
        self.config_dir = Path(config_dir)
        self.threshold = threshold
        self.pause = pause
        self.runner = runner
        self.timeout = timeout

    def job(self, track: SourceTrack, staging_dir: Path) -> dict:
        """Everything the worker process needs, as plain values."""
        return {
            "artist": track.artist,
            "title": track.title,
            "album": track.album,
            "duration": track.duration,
            "isrc": track.isrc,
            "mbid": track.mbid,
            "staging_dir": str(staging_dir),
            "config_dir": str(self.config_dir.resolve()),
            "threshold": self.threshold,
            "pause": min(self.pause, 3.0),
        }

    async def download(self, track: SourceTrack, staging_dir: Path) -> DownloadResult:
        problem, details = None, {}
        try:
            result = await asyncio.wait_for(self.runner(self.job(track, staging_dir)), self.timeout)
        except TimeoutError:
            problem = f"The download took longer than {round(self.timeout / 60)} minutes."
        except WorkerFailed as exc:
            problem = "The download process stopped unexpectedly."
            details = {"excerpt": str(exc)[-500:]}
        await asyncio.sleep(self.pause)  # be gentle with YouTube between downloads
        if problem:
            raise DownloaderError(problem, **details)
        if not result.get("ok"):
            raise DownloaderError(
                result.get("message") or "The download failed.",
                tried=result.get("tried"),
                yt_dlp_version=result.get("yt_dlp_version"),
            )
        return DownloadResult(
            path=Path(result["path"]),
            format=result["format"],
            bitrate=result.get("bitrate"),
            downloader=self.name,
            origin=result.get("origin"),
            duration=result.get("duration"),
            cover_url=result.get("cover_url"),
            album=result.get("album"),
            tried=result.get("tried") or [],
        )
