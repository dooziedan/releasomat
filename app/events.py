"""Live updates: a small in-process event bus, streamed to browsers as Server-Sent Events.

The pipeline and the worker ``publish`` events ("run 12 started", "run 12 failed: …"). Every open
Activity or Playlists page listens through ``GET /api/events`` (``routes/activity.py``); htmx's
SSE extension reloads the affected list when an event arrives.

Everything here runs in the server's event loop: call ``publish`` from async code (the pipeline,
the worker, the scheduler's jobs), not from a thread.
"""

import asyncio
import json
import signal
import threading
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from itertools import count

KEEPALIVE = 15  # seconds between "still here" comments while nothing happens
QUEUE_SIZE = 200  # events kept per listener; a stuck listener misses newer ones


@dataclass(frozen=True)
class Event:
    id: int
    type: str  # "run": a run started, changed or ended
    data: dict


_CLOSED = Event(0, "closed", {})


class EventBus:
    def __init__(self) -> None:
        self._listeners: set[asyncio.Queue[Event]] = set()
        self._ids = count(1)
        self.closed = False

    def publish(self, type: str, **data) -> Event:
        """Send an event to everyone listening right now (nothing is stored)."""
        event = Event(next(self._ids), type, data)
        for queue in list(self._listeners):
            # A stuck listener misses events. Pages reload whole lists on the next one anyway.
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)
        return event

    async def listen(self) -> AsyncIterator[Event | None]:
        """Events as they happen, and None every KEEPALIVE seconds when nothing happened.
        Ends when the bus is closed (server stopping)."""
        queue: asyncio.Queue[Event] = asyncio.Queue(QUEUE_SIZE)
        self._listeners.add(queue)
        try:
            while not self.closed:
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE)
                except TimeoutError:
                    yield None
                    continue
                if event is _CLOSED:
                    return
                yield event
        finally:
            self._listeners.discard(queue)

    @property
    def listeners(self) -> int:
        return len(self._listeners)

    def close(self) -> None:
        """End every open stream."""
        self.closed = True
        for queue in list(self._listeners):
            while queue.full():
                queue.get_nowait()
            queue.put_nowait(_CLOSED)

    def reopen(self) -> None:
        """After a restart in the same process (tests start the app many times)."""
        self.closed = False


bus = EventBus()


def sse(event: Event | None) -> str:
    """One Server-Sent Events message; a keep-alive comment for None."""
    if event is None:
        return ": keep-alive\n\n"
    return f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(event.data, default=str)}\n\n"


def close_streams_on_exit(events: EventBus = bus) -> None:
    """Make Ctrl+C / ``docker stop`` end the open event streams.

    The server waits for open responses before it stops, and an event stream never ends by
    itself. So when the stop signal arrives, close the streams first, then let the server's own
    signal handler do its work. Only possible in the main thread (not in tests).
    """
    if threading.current_thread() is not threading.main_thread():
        return
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(number)
        if not callable(previous):
            continue

        def handler(signum, frame, previous=previous):
            loop.call_soon_threadsafe(events.close)
            previous(signum, frame)

        signal.signal(number, handler)
