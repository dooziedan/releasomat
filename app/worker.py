"""The queue of refreshes, the download limit and the Stop button (ADR 0005).

- A playlist never refreshes twice at the same time: while it waits in the queue or refreshes,
  another request for it is turned down. A scheduled one is recorded as a "skipped" run, so the
  Activity page shows why it didn't run.
- At most ``REFRESH_WORKERS`` playlists refresh at the same time.
- Downloads of all refreshes share ``downloads``: at most DOWNLOAD_CONCURRENCY at once (a change
  of that setting applies after a restart).
- **Stop** (``stop_refresh``): a refresh that still waits in the queue is dropped; a running one
  is stopped at once (``pipeline.StopButton``) and its run ends as "stopped". The playlist is free
  again as soon as it has ended, so Refresh now works right away.

The worker lives in the server's event loop: call its methods from async code.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import Engine
from sqlmodel import Session

from app import pipeline
from app.events import EventBus, bus
from app.models import Playlist, Run, RunStatus, Trigger, utcnow
from app.pipeline import StopButton

log = logging.getLogger(__name__)

REFRESH_WORKERS = 2
STOP_WAIT = 15.0  # seconds ``stop_refresh`` waits for a stopped refresh to end

SKIPPED_MESSAGE = "Skipped: this playlist was still refreshing from before."
DROPPED_MESSAGE = "Stopped by you before it started."


@dataclass
class Job:
    """One requested refresh, from the queue until it has ended."""

    playlist_id: int
    trigger: str
    stop: StopButton = field(default_factory=StopButton)
    dropped: bool = False  # stopped while it waited in the queue: it never starts
    ended: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class StopResult:
    """What pressing Stop did. ``outcome`` is one of:

    - ``stopped``: the refresh was stopped and has ended (its run says "stopped")
    - ``dropped``: it was still waiting in the queue and won't start
    - ``stopping``: it is still ending (a download takes a few seconds to cancel)
    - ``too_late``: it is saving the Navidrome playlist and ends by itself in a moment
    - ``not_running``: nothing to stop
    """

    outcome: str
    message: str
    run_id: int | None = None


class Worker:
    def __init__(
        self,
        engine: Engine,
        *,
        refresh: Callable[[int, str, StopButton], Awaitable] | None = None,
        workers: int = REFRESH_WORKERS,
        download_limit: int = 2,
        events: EventBus = bus,
    ):
        self.engine = engine
        self.refresh = refresh or self._refresh
        self.workers = workers
        self.events = events
        self.downloads = asyncio.Semaphore(download_limit)
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._waiting: dict[int, Job] = {}  # by playlist ID
        self._running: dict[int, Job] = {}
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for number in range(self.workers):
            self._tasks.append(asyncio.create_task(self._work(), name=f"refresh-worker-{number}"))

    async def stop(self) -> None:
        """Releasomat is shutting down: cut every refresh off (``mark_interrupted`` records them
        at the next start)."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def state(self, playlist_id: int) -> str | None:
        """ "waiting", "running", "stopping" (Stop was pressed, it is ending) or None (idle)."""
        job = self._running.get(playlist_id)
        if job is not None:
            return "stopping" if job.stop.pressed else "running"
        return "waiting" if playlist_id in self._waiting else None

    def running_run(self, playlist_id: int) -> int | None:
        """The ID of the run the playlist's refresh is working on right now, if any."""
        job = self._running.get(playlist_id)
        return job.stop.run_id if job else None

    def request_refresh(self, playlist_id: int, trigger: str) -> bool:
        """Queue a refresh. False when the playlist is already waiting or refreshing."""
        if self.state(playlist_id) is not None:
            log.info(
                "Refresh of playlist %s (%s) skipped: already refreshing", playlist_id, trigger
            )
            if trigger == Trigger.SCHEDULE:
                self._record_skipped(playlist_id, trigger)
            return False
        job = Job(playlist_id, trigger)
        self._waiting[playlist_id] = job
        self._queue.put_nowait(job)
        return True

    async def stop_refresh(self, playlist_id: int, *, wait: float = STOP_WAIT) -> StopResult:
        """The Stop button: drop the playlist's waiting refresh, or stop its running one and
        wait (at most ``wait`` seconds) until it has ended."""
        job = self._waiting.pop(playlist_id, None)
        if job is not None:
            job.dropped = True
            job.ended.set()
            log.info("Waiting refresh of playlist %s dropped by the owner", playlist_id)
            self.events.publish(
                "run", run_id=None, playlist_id=playlist_id, status=RunStatus.STOPPED.value,
                message=DROPPED_MESSAGE,
            )  # fmt: skip
            return StopResult("dropped", "The waiting refresh was dropped; it won't start.")
        job = self._running.get(playlist_id)
        if job is None:
            return StopResult("not_running", "Nothing to stop: this playlist isn't refreshing.")
        if not job.stop.press():
            return StopResult(
                "too_late",
                "Too late to stop: the refresh is saving the Navidrome playlist and ends in a "
                "moment.",
                job.stop.run_id,
            )
        log.info("Refresh of playlist %s: stopping (the owner pressed Stop)", playlist_id)
        try:
            await asyncio.wait_for(job.ended.wait(), wait)
        except TimeoutError:
            message = "Stopping: the current download is being ended."
            return StopResult("stopping", message, job.stop.run_id)
        status, message = self._run_outcome(job.stop.run_id)
        if status not in (None, RunStatus.STOPPED):  # it had just finished by itself
            message = f"Too late to stop, it had just ended: {message}"
            return StopResult("too_late", message, job.stop.run_id)
        return StopResult("stopped", message, job.stop.run_id)

    async def join(self) -> None:
        """Wait until the queue is empty and every refresh has finished (used by tests)."""
        await self._queue.join()

    async def _refresh(self, playlist_id: int, trigger: str, stop: StopButton) -> None:
        await pipeline.refresh_playlist(
            playlist_id,
            trigger,
            engine=self.engine,
            events=self.events,
            slots=self.downloads,
            stop=stop,
        )

    async def _work(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if not job.dropped:
                    await self._run(job)
            finally:
                self._queue.task_done()

    async def _run(self, job: Job) -> None:
        """Run one refresh as its own task, so the Stop button can cancel it alone."""
        playlist_id = job.playlist_id
        if self._waiting.get(playlist_id) is job:
            del self._waiting[playlist_id]
        self._running[playlist_id] = job
        job.stop.task = asyncio.create_task(
            self.refresh(playlist_id, job.trigger, job.stop), name=f"refresh-{playlist_id}"
        )
        try:
            await job.stop.task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # Releasomat is shutting down, not a Stop
            log.info("Refresh of playlist %s stopped by the owner", playlist_id)
            if job.stop.run_id is None:  # stopped before its run began: pages update anyway
                self.events.publish(
                    "run", run_id=None, playlist_id=playlist_id, status=RunStatus.STOPPED.value,
                    message=DROPPED_MESSAGE,
                )  # fmt: skip
        except Exception:  # the pipeline records its own errors; this is a last resort
            log.exception("Refresh of playlist %s crashed", playlist_id)
        finally:
            del self._running[playlist_id]  # the playlist is free again
            job.ended.set()

    def _run_outcome(self, run_id: int | None) -> tuple[str | None, str]:
        """The status and message a run ended with (none yet: stopped before it began)."""
        with Session(self.engine) as session:
            run = session.get(Run, run_id) if run_id else None
            if run is None:
                return None, DROPPED_MESSAGE
            return run.status, run.message or ""

    def _record_skipped(self, playlist_id: int, trigger: str) -> None:
        with Session(self.engine) as session:
            if session.get(Playlist, playlist_id) is None:
                return
            now = utcnow()
            run = Run(
                playlist_id=playlist_id,
                trigger=trigger,
                status=RunStatus.SKIPPED.value,
                started_at=now,
                finished_at=now,
                message=SKIPPED_MESSAGE,
            )
            run.add_log(SKIPPED_MESSAGE)
            session.add(run)
            session.commit()
            run_id = run.id
        self.events.publish(
            "run", run_id=run_id, playlist_id=playlist_id, status=RunStatus.SKIPPED.value
        )
