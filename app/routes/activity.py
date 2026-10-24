"""Activity: the runs (refreshes), their Stop button, and the live event stream that keeps pages
up to date."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from sqlmodel import Session, select

from app import events, reports
from app.db import SessionDep
from app.models import Playlist, Run
from app.routes.playlists import STOP_STATUS
from app.templating import templates
from app.worker import StopResult

router = APIRouter()


def _runs(session: Session, playlist_id: int | None = None, limit: int = 100) -> list[Run]:
    query = select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(limit)
    if playlist_id is not None:
        query = query.where(Run.playlist_id == playlist_id)
    return list(session.exec(query).all())


def _titles(session: Session) -> dict[int, str]:
    return {p.id: p.title for p in session.exec(select(Playlist)).all()}


def _get_run(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    return run


async def _stop(request: Request, run: Run) -> StopResult:
    """Stop a run, if it is the one its playlist's refresh is working on."""
    worker = request.app.state.worker
    if worker.running_run(run.playlist_id) != run.id:
        return StopResult("not_running", "Nothing to stop: this run isn't running.", run.id)
    return await worker.stop_refresh(run.playlist_id)


@router.get("/api/runs", tags=["activity"])
def list_runs(
    session: SessionDep,
    playlist_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[Run]:
    """The latest runs, newest first (optionally of one playlist)."""
    return _runs(session, playlist_id, limit)


@router.get("/api/runs/{run_id}", tags=["activity"])
def get_run(run_id: int, session: SessionDep) -> Run:
    return _get_run(session, run_id)


@router.post("/api/runs/{run_id}/stop", tags=["activity"])
async def stop_run(
    run_id: int, request: Request, response: Response, session: SessionDep
) -> StopResult:
    """Stop this run now (the Stop button on the Activity page). Works like
    ``POST /api/playlists/{playlist_id}/stop``: 200 when it has ended as "stopped", 202 when it
    is still ending, 409 when it isn't running (``not_running``) or it is too late
    (``too_late``). 404 when there is no such run."""
    result = await _stop(request, _get_run(session, run_id))
    response.status_code = STOP_STATUS[result.outcome]
    return result


@router.get("/api/runs/{run_id}/report", tags=["activity"], response_class=PlainTextResponse)
def get_run_report(run_id: int, session: SessionDep) -> str:
    """A run's error report as plain text (what the Copy report button copies). 404 when the
    run has no report."""
    run = session.get(Run, run_id)
    if run is None or not run.report:
        raise HTTPException(404, "This run has no error report")
    playlist = session.get(Playlist, run.playlist_id)
    return reports.as_text(run.report, playlist.title if playlist else None)


@router.get("/api/events", tags=["activity"], response_class=StreamingResponse)
async def stream_events(request: Request) -> StreamingResponse:
    """Live events as Server-Sent Events (``text/event-stream``).

    ``event: run`` with ``data: {"run_id", "playlist_id", "status", "message"?}`` whenever a run
    starts, makes progress, is skipped, stopped or ends (``run_id`` is null when a refresh was
    stopped before its run began); ``event: track-<playlist ID>`` with
    ``data: {"run_id", "track_id", "position", "status"}`` whenever a track of that playlist
    changes during a refresh. A comment line every 15 seconds keeps the connection open.
    """

    async def stream():
        yield "retry: 5000\n\n"  # the browser reconnects after 5 s if the connection drops
        async for event in events.bus.listen():
            if await request.is_disconnected():
                break
            yield events.sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _rows_context(session: Session) -> dict:
    return {"runs": _runs(session), "titles": _titles(session), "stop_buttons": True}


@router.get("/activity", response_class=HTMLResponse, include_in_schema=False)
def activity_page(request: Request, session: SessionDep):
    return templates.TemplateResponse(
        request,
        "activity.html",
        {"page": "activity", "versions": reports.versions(), **_rows_context(session)},
    )


@router.get("/partials/runs", response_class=HTMLResponse, include_in_schema=False)
def runs_partial(request: Request, session: SessionDep):
    """The runs table again, reloaded by the Activity page on every run event."""
    return templates.TemplateResponse(request, "partials/run_rows.html", _rows_context(session))


@router.post("/runs/{run_id}/stop", response_class=HTMLResponse, include_in_schema=False)
async def stop_run_form(run_id: int, request: Request, session: SessionDep):
    """Stop on the Activity page: htmx gets the runs table back once the run has ended;
    without htmx, the page it came from."""
    await _stop(request, _get_run(session, run_id))
    if request.headers.get("HX-Request"):
        session.expire_all()  # the run changed meanwhile: read it again
        return templates.TemplateResponse(request, "partials/run_rows.html", _rows_context(session))
    return RedirectResponse(request.headers.get("referer") or "/activity", status_code=303)
