"""The event bus and the Server-Sent Events stream."""

import asyncio

from app import events
from app.events import EventBus, sse


async def test_listeners_get_published_events():
    bus = EventBus()
    received = []

    async def listen():
        async for event in bus.listen():
            received.append(event)

    task = asyncio.create_task(listen())
    await asyncio.sleep(0)
    assert bus.listeners == 1
    bus.publish("run", run_id=1, status="running")
    bus.publish("run", run_id=1, status="failed")
    await asyncio.sleep(0)
    bus.close()  # ends every stream (the server is stopping)
    await asyncio.wait_for(task, 1)
    assert [(e.type, e.data["status"]) for e in received] == [("run", "running"), ("run", "failed")]
    assert bus.listeners == 0


def test_sse_format():
    event = events.Event(7, "run", {"run_id": 3, "status": "done"})
    assert sse(event) == 'id: 7\nevent: run\ndata: {"run_id": 3, "status": "done"}\n\n'
    assert sse(None) == ": keep-alive\n\n"


def test_event_stream_endpoint(client):
    events.bus.close()  # so the stream ends right away instead of waiting for events
    response = client.get("/api/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("retry: 5000")


def test_pages_listen_for_live_updates(client):
    for path in ("/", "/activity"):
        page = client.get(path).text
        assert 'sse-connect="/api/events"' in page and 'hx-trigger="sse:run delay:' in page
    playlist = client.post(
        "/api/playlists", json={"url": "https://www.deezer.com/playlist/908622995"}
    ).json()
    page = client.get(f"/playlists/{playlist['id']}").text
    assert f"sse:track-{playlist['id']} delay:" in page  # the tracks table follows its tracks
