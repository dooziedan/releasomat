"""Playlists: the list, adding one (with source detection), a playlist's page and settings,
Refresh now, and the "Don't like" tracks (all of them on one page, and Undo).

The JSON API under ``/api/playlists`` is documented at /docs; the pages and htmx partials below it
use the same functions from ``app/playlists.py``.
"""

from contextlib import suppress
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, or_, select

from app import lifecycle, playlists
from app import settings as user_settings
from app.config import get_config
from app.db import SessionDep
from app.models import (
    FileStatus,
    NavidromeMode,
    Playlist,
    PlaylistTrack,
    Run,
    Track,
    TrackFile,
    Trigger,
)
from app.pipeline import file_of
from app.scheduler import PRESETS, WEEKDAYS, InvalidSchedule, Schedule, next_run
from app.sources import SOURCES
from app.sources.base import PlaylistRef, UnsupportedLink, detect_source
from app.sources.listenbrainz import SERIES, series_ref
from app.templating import templates
from app.worker import StopResult

router = APIRouter()

# The HTTP status of a Stop answer, by StopResult.outcome.
STOP_STATUS = {"stopped": 200, "dropped": 200, "stopping": 202, "too_late": 409, "not_running": 409}


# --- Shared helpers -------------------------------------------------------------------------


def _get(session: Session, playlist_id: int) -> Playlist:
    playlist = session.get(Playlist, playlist_id)
    if playlist is None:
        raise HTTPException(404, "No such playlist")
    return playlist


def _ref(session: Session, url: str | None, series: str | None, user: str | None) -> PlaylistRef:
    """The playlist to add: a pasted link, or a ListenBrainz "Created for you" series."""
    if series:
        return series_ref(user or user_settings.get(session, "LISTENBRAINZ_USER"), series)
    return detect_source(url or "")


def _state(request: Request, playlist_id: int) -> str | None:
    """ "waiting" or "running" while the worker has the playlist, else None."""
    return request.app.state.worker.state(playlist_id)


def _sync_schedule(request: Request, playlist_id: int) -> None:
    request.app.state.scheduler.sync_playlist(playlist_id)


def _rows(request: Request, session: Session) -> list[dict]:
    rows = []
    for playlist in session.exec(select(Playlist).order_by(Playlist.title)).all():
        rows.append(
            {
                "playlist": playlist,
                "counts": playlists.track_counts(session, playlist.id),
                "state": _state(request, playlist.id),
            }
        )
    return rows


def _last_runs(session: Session, playlist_id: int, limit: int = 5) -> list[Run]:
    return list(
        session.exec(
            select(Run)
            .where(Run.playlist_id == playlist_id)
            .order_by(Run.started_at.desc(), Run.id.desc())
            .limit(limit)
        ).all()
    )


def _tracks(session: Session, playlist_id: int) -> list[dict]:
    """The playlist's current tracks in order, each with the state of its file."""
    entries = session.exec(
        select(PlaylistTrack, Track)
        .join(Track, Track.id == PlaylistTrack.track_id)
        .where(PlaylistTrack.playlist_id == playlist_id, PlaylistTrack.removed_at.is_(None))
        .order_by(PlaylistTrack.position)
    ).all()
    files = {}
    track_ids = [track.id for _, track in entries]
    if track_ids:
        query = select(TrackFile).where(TrackFile.track_id.in_(track_ids)).order_by(TrackFile.id)
        for file in session.exec(query):
            files.setdefault(file.track_id, file)
    rows = []
    for entry, track in entries:
        file = files.get(track.id)
        details = (file.details if file else None) or {}
        rows.append(
            {
                "track_id": track.id,
                "position": entry.position,
                "artist": track.artist,
                "title": track.title,
                "album": track.album,
                "duration": track.duration,
                "isrc": track.isrc,
                "mbid": track.mbid,
                "status": file.status if file else "pending",
                "downloader": file.downloader if file else None,
                "format": file.format if file else None,
                "bitrate": file.bitrate if file else None,
                "path": file.path if file else None,
                "navidrome_song_id": file.navidrome_song_id if file else None,
                "error": file.error if file else None,
                "matched_by": details.get("matched_by"),
                "origin": details.get("origin"),
                "tried": details.get("tried") or [],
                "reasons": details.get("reasons") or [],  # the error, one per download source
                "added_at": entry.added_at,
                # What happened to a downloaded file (ADR 0009, app/lifecycle.py).
                "former_path": file.former_path if file else None,
                "moved": bool(details.get("moved")),
                "adopted_at": file.adopted_at if file else None,
                "disliked_at": file.disliked_at if file else None,
                "disliked_reason": file.disliked_reason if file else None,
            }
        )
    return rows


def _disliked(session: Session) -> list[dict]:
    """Every "Don't like" track, newest first, with the playlists that contain it now. The same
    tracks as the "Don't like" badges on the playlist pages, also those linked from the library."""
    found = session.exec(
        select(TrackFile, Track)
        .join(Track, Track.id == TrackFile.track_id)
        .where(or_(TrackFile.disliked_at.is_not(None), TrackFile.status == FileStatus.DISLIKED))
        .order_by(TrackFile.disliked_at.desc(), Track.artist, Track.title)
    ).all()
    lists: dict[int, list[dict]] = {}
    if found:
        entries = session.exec(
            select(PlaylistTrack.track_id, Playlist.id, Playlist.title)
            .join(Playlist, Playlist.id == PlaylistTrack.playlist_id)
            .where(
                PlaylistTrack.track_id.in_([track.id for _, track in found]),
                PlaylistTrack.removed_at.is_(None),
            )
            .order_by(Playlist.title)
        ).all()
        for track_id, playlist_id, title in entries:
            mine = lists.setdefault(track_id, [])
            if all(p["id"] != playlist_id for p in mine):  # a track can be in a playlist twice
                mine.append({"id": playlist_id, "title": title})
    return [
        {
            "track_id": track.id,
            "artist": track.artist,
            "title": track.title,
            "album": track.album,
            "duration": track.duration,
            "status": file.status,  # "in_library": linked from the library anyway
            "disliked_at": file.disliked_at,
            "disliked_reason": file.disliked_reason,
            "former_path": file.former_path,
            "playlists": lists.get(track.id, []),
        }
        for file, track in found
    ]


def _undo_dislike(session: Session, track_id: int) -> tuple[TrackFile, lifecycle.Result]:
    """Undo "Don't like" for a track (404 if Releasomat has no file row for it)."""
    file = file_of(session, track_id)
    if file is None:
        raise HTTPException(404, "No such track, or nothing to undo")
    return file, lifecycle.undo_dislike(session, file)


# --- JSON API -------------------------------------------------------------------------------


class NewPlaylist(BaseModel):
    """A playlist link, or a ListenBrainz "Created for you" series (then ``url`` is not needed).
    ``listenbrainz_user`` defaults to the LISTENBRAINZ_USER setting."""

    url: str | None = None
    listenbrainz_series: str | None = None
    listenbrainz_user: str | None = None


class PlaylistChanges(BaseModel):
    """Only the fields that are sent are changed. Which download sources are used is one global
    setting for all playlists (``DOWNLOAD_SOURCES`` in /api/settings), not a playlist's."""

    model_config = ConfigDict(extra="forbid")  # e.g. the old "downloaders" gets a clear 422

    enabled: bool | None = None
    schedule: str | None = None  # cron expression, "" = only Refresh now
    navidrome_mode: NavidromeMode | None = None
    snapshot_keep: int | None = None


class Detection(BaseModel):
    source: str
    source_label: str
    kind: str
    id: str
    url: str
    label: str


class SchedulePreview(BaseModel):
    cron: str
    description: str
    next_refresh: datetime | None
    time_zone: str


@router.get("/api/playlists", tags=["playlists"])
def list_playlists(session: SessionDep) -> list[Playlist]:
    return list(session.exec(select(Playlist).order_by(Playlist.title)).all())


@router.post("/api/playlists", tags=["playlists"], status_code=201)
def add_playlist(new: NewPlaylist, request: Request, session: SessionDep) -> Playlist:
    """Follow a playlist. 422 when the link isn't a playlist Releasomat knows, 409 when it is
    followed already."""
    try:
        ref = _ref(session, new.url, new.listenbrainz_series, new.listenbrainz_user)
        playlist = playlists.add(session, ref)
    except UnsupportedLink as exc:
        raise HTTPException(422, str(exc)) from None
    except playlists.AlreadyFollowed as exc:
        raise HTTPException(409, str(exc)) from None
    _sync_schedule(request, playlist.id)
    return playlist


@router.get("/api/playlists/{playlist_id}", tags=["playlists"])
def get_playlist(playlist_id: int, session: SessionDep) -> Playlist:
    return _get(session, playlist_id)


@router.patch("/api/playlists/{playlist_id}", tags=["playlists"])
def change_playlist(
    playlist_id: int, changes: PlaylistChanges, request: Request, session: SessionDep
) -> Playlist:
    """Change a playlist's settings; its schedule takes effect at once."""
    playlist = _get(session, playlist_id)
    try:
        playlists.update(session, playlist, changes.model_dump(exclude_unset=True))
    except playlists.InvalidSettings as exc:
        raise HTTPException(422, exc.errors) from None
    _sync_schedule(request, playlist_id)
    session.refresh(playlist)
    return playlist


@router.delete("/api/playlists/{playlist_id}", tags=["playlists"], status_code=204)
def delete_playlist(playlist_id: int, request: Request, session: SessionDep) -> Response:
    """Stop following a playlist. Downloaded files stay where they are."""
    playlist = _get(session, playlist_id)
    if _state(request, playlist_id):
        raise HTTPException(409, "The playlist is refreshing. Try again when it is done.")
    playlists.remove(session, playlist)
    _sync_schedule(request, playlist_id)
    return Response(status_code=204)


@router.post("/api/playlists/{playlist_id}/refresh", tags=["playlists"], status_code=202)
async def refresh_playlist(
    playlist_id: int, request: Request, response: Response, session: SessionDep
) -> dict:
    """Refresh now. 409 when the playlist is already waiting or refreshing."""
    # async: the worker's queue lives in the event loop (app/worker.py).
    _get(session, playlist_id)
    queued = request.app.state.worker.request_refresh(playlist_id, Trigger.MANUAL.value)
    if not queued:
        response.status_code = 409
    return {"queued": queued, "state": _state(request, playlist_id)}


@router.post("/api/playlists/{playlist_id}/stop", tags=["playlists"])
async def stop_playlist_refresh(
    playlist_id: int, request: Request, response: Response, session: SessionDep
) -> StopResult:
    """Stop the playlist's refresh now (the Stop button).

    A refresh that still waits in the queue is dropped (``dropped``). A running one ends at once:
    the current download is cancelled, tracks already saved stay, the Navidrome playlist is left
    as it is, and the run ends as "stopped". The answer comes when it has ended (``stopped``,
    200), or after 15 seconds if it is still ending (``stopping``, 202). 409 when nothing is
    running (``not_running``) or when it is too late (``too_late``: it is saving the Navidrome
    playlist and ends by itself in a moment)."""
    _get(session, playlist_id)
    result = await request.app.state.worker.stop_refresh(playlist_id)
    response.status_code = STOP_STATUS[result.outcome]
    return result


@router.get("/api/playlists/{playlist_id}/tracks", tags=["playlists"])
def playlist_tracks(playlist_id: int, session: SessionDep) -> list[dict]:
    """The playlist's tracks in order, with the status of each one's file."""
    _get(session, playlist_id)
    return _tracks(session, playlist_id)


class UndoResult(BaseModel):
    track_id: int
    changed: bool  # False when the track wasn't "Don't like" (nothing to undo)
    status: str  # the track's state now: "pending" (missing: downloaded by the next refresh)


@router.delete("/api/tracks/{track_id}/dislike", tags=["playlists"])
def undo_dislike(track_id: int, session: SessionDep) -> UndoResult:
    """Undo "Don't like": the track is a normal missing track again, and the next refresh
    downloads it (a song linked from the library stays linked). 404 for an unknown track."""
    file, result = _undo_dislike(session, track_id)
    if not result.ok:
        raise HTTPException(409, result.reason)
    return UndoResult(track_id=track_id, changed=result.changed, status=file.status)


@router.get("/api/tracks/disliked", tags=["playlists"])
def disliked_tracks(session: SessionDep) -> list[dict]:
    """Every "Don't like" track, newest first: why, since when, and the playlists that contain
    it now. Releasomat never downloads them again (one that is in the library anyway has
    ``status`` "in_library": it stays linked). Undo: ``DELETE /api/tracks/{track_id}/dislike``."""
    return _disliked(session)


@router.get("/api/sources/detect", tags=["playlists"])
def detect(url: str) -> Detection:
    """Which source and playlist a link points to (no network involved). 422 with a reason
    when Releasomat doesn't know the link."""
    try:
        ref = detect_source(url)
    except UnsupportedLink as exc:
        raise HTTPException(422, str(exc)) from None
    return Detection(source_label=SOURCES[ref.source].label, **asdict(ref))


@router.get("/api/schedule/preview", tags=["playlists"])
def schedule_preview(cron: str = "") -> SchedulePreview:
    """When a cron expression would refresh next, in the container's time zone (TZ)."""
    zone = get_config().zone
    try:
        upcoming = next_run(cron, zone)
    except InvalidSchedule as exc:
        raise HTTPException(422, str(exc)) from None
    description = Schedule.from_cron(cron).describe()
    return SchedulePreview(
        cron=cron.strip(), description=description, next_refresh=upcoming, time_zone=zone.key
    )


# --- Pages and partials ---------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def playlists_page(request: Request, session: SessionDep):
    return templates.TemplateResponse(
        request, "playlists.html", {"rows": _rows(request, session), "page": "playlists"}
    )


@router.get("/partials/playlists", response_class=HTMLResponse, include_in_schema=False)
def playlists_partial(request: Request, session: SessionDep):
    """The list again, reloaded by the page when a run starts or ends (Server-Sent Events)."""
    return templates.TemplateResponse(
        request, "partials/playlist_rows.html", {"rows": _rows(request, session)}
    )


def _new_page(request: Request, session: Session, errors=None, form=None, status_code=200):
    return templates.TemplateResponse(
        request,
        "playlist_new.html",
        {
            "page": "new",
            "errors": errors or {},
            "form": form or {},
            "sources": SOURCES,
            "series": SERIES,
            "listenbrainz_user": user_settings.get(session, "LISTENBRAINZ_USER"),
        },
        status_code=status_code,
    )


@router.get("/playlists/new", response_class=HTMLResponse, include_in_schema=False)
def new_playlist_page(request: Request, session: SessionDep):
    return _new_page(request, session)


@router.post("/partials/detect", response_class=HTMLResponse, include_in_schema=False)
async def detect_partial(request: Request):
    """What the pasted link is, shown below the field while typing (htmx)."""
    form = await request.form()
    url = str(form.get("url", "")).strip()
    context = {"url": url, "ref": None, "error": None}
    if url:
        try:
            context["ref"] = detect_source(url)
        except UnsupportedLink as exc:
            context["error"] = str(exc)
    return templates.TemplateResponse(request, "partials/detected.html", context)


@router.post("/playlists", response_class=HTMLResponse, include_in_schema=False)
async def add_playlist_form(request: Request, session: SessionDep):
    form = await request.form()
    values = {key: str(value) for key, value in form.items()}
    series = values.get("listenbrainz_series") if values.get("kind") == "series" else None
    field = "series" if series else "url"
    try:
        ref = _ref(session, values.get("url"), series, values.get("listenbrainz_user"))
        playlist = playlists.add(session, ref)
    except UnsupportedLink as exc:
        return _new_page(request, session, {field: str(exc)}, values, status_code=422)
    except playlists.AlreadyFollowed as exc:
        return RedirectResponse(f"/playlists/{exc.playlist.id}?already=1", status_code=303)
    _sync_schedule(request, playlist.id)
    return RedirectResponse(f"/playlists/{playlist.id}?added=1", status_code=303)


def _schedule_context(schedule: Schedule) -> dict:
    """The schedule fields plus the next refresh (or what is wrong with the schedule)."""
    zone = get_config().zone
    context = {
        "schedule": schedule,
        "presets": PRESETS,
        "weekdays": WEEKDAYS,
        "zone": zone.key,
        "upcoming": None,
        "schedule_error": None,
    }
    try:
        context["upcoming"] = next_run(schedule.to_cron(), zone)
    except InvalidSchedule as exc:
        context["schedule_error"] = str(exc)
    return context


def _playlist_page(request, session, playlist, schedule=None, errors=None, status_code=200):
    return templates.TemplateResponse(
        request,
        "playlist.html",
        {
            "page": "playlist",
            "playlist": playlist,
            "state": _state(request, playlist.id),
            "tracks": _tracks(session, playlist.id),
            "runs": _last_runs(session, playlist.id),
            "download_sources": user_settings.describe_sources(
                user_settings.get_list(session, "DOWNLOAD_SOURCES"),
                user_settings.slskd_set_up(session),
            ),
            "snapshot_auto_delete": user_settings.get_bool(session, "SNAPSHOT_AUTO_DELETE"),
            "errors": errors or {},
            **_schedule_context(schedule or Schedule.from_cron(playlist.schedule)),
        },
        status_code=status_code,
    )


@router.get("/playlists/{playlist_id}", response_class=HTMLResponse, include_in_schema=False)
def playlist_page(playlist_id: int, request: Request, session: SessionDep):
    return _playlist_page(request, session, _get(session, playlist_id))


@router.get(
    "/partials/playlists/{playlist_id}/status",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def playlist_status_partial(playlist_id: int, request: Request, session: SessionDep):
    """Status, Refresh now and the latest runs, reloaded live while the page is open."""
    playlist = _get(session, playlist_id)
    return templates.TemplateResponse(
        request,
        "partials/playlist_status.html",
        {
            "playlist": playlist,
            "state": _state(request, playlist_id),
            "runs": _last_runs(session, playlist_id),
        },
    )


@router.get(
    "/partials/playlists/{playlist_id}/tracks",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def playlist_tracks_partial(playlist_id: int, request: Request, session: SessionDep):
    """The tracks table again, reloaded live while a refresh changes the tracks."""
    playlist = _get(session, playlist_id)
    return templates.TemplateResponse(
        request,
        "partials/tracks.html",
        {"playlist": playlist, "tracks": _tracks(session, playlist_id)},
    )


def _schedule_from_form(form) -> Schedule:
    return Schedule(
        preset=str(form.get("schedule_preset", "off")),
        time=str(form.get("schedule_time", "06:00")) or "06:00",
        weekday=str(form.get("schedule_weekday", "mon")),
        cron=str(form.get("schedule_cron", "")),
    )


@router.post("/partials/schedule", response_class=HTMLResponse, include_in_schema=False)
async def schedule_partial(request: Request):
    """The schedule fields for the chosen preset, with the next refresh (htmx)."""
    form = await request.form()
    schedule = _schedule_from_form(form)
    if schedule.preset == "custom" and "schedule_cron" not in form:
        # Just switched to custom: start from the schedule that was shown before.
        before = "weekly" if "schedule_weekday" in form else "daily"
        with suppress(InvalidSchedule):
            schedule.cron = Schedule(before, schedule.time, schedule.weekday).to_cron()
    return templates.TemplateResponse(
        request, "partials/schedule.html", _schedule_context(schedule)
    )


@router.post(
    "/playlists/{playlist_id}/settings", response_class=HTMLResponse, include_in_schema=False
)
async def save_playlist_settings(playlist_id: int, request: Request, session: SessionDep):
    playlist = _get(session, playlist_id)
    form = await request.form()
    schedule = _schedule_from_form(form)
    errors = {}
    changes = {
        "enabled": form.get("enabled") == "on",
        "navidrome_mode": form.get("navidrome_mode", NavidromeMode.UPDATE),
        "snapshot_keep": form.get("snapshot_keep", playlist.snapshot_keep),
    }
    try:
        changes["schedule"] = schedule.to_cron()
    except InvalidSchedule as exc:
        errors["schedule"] = str(exc)
    if not errors:
        try:
            playlists.update(session, playlist, changes)
        except playlists.InvalidSettings as exc:
            errors = exc.errors
    if errors:
        session.refresh(playlist)
        return _playlist_page(request, session, playlist, schedule, errors, status_code=422)
    _sync_schedule(request, playlist_id)
    return RedirectResponse(f"/playlists/{playlist_id}?saved=1", status_code=303)


@router.post(
    "/playlists/{playlist_id}/refresh", response_class=HTMLResponse, include_in_schema=False
)
async def refresh_form(playlist_id: int, request: Request, session: SessionDep):
    """Refresh now: htmx gets the button back (now "Waiting…"); without htmx, the page."""
    _get(session, playlist_id)
    request.app.state.worker.request_refresh(playlist_id, Trigger.MANUAL.value)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "partials/refresh_button.html",
            {"playlist_id": playlist_id, "state": _state(request, playlist_id)},
        )
    back = request.headers.get("referer") or f"/playlists/{playlist_id}"
    return RedirectResponse(back, status_code=303)


@router.post("/playlists/{playlist_id}/stop", response_class=HTMLResponse, include_in_schema=False)
async def stop_form(playlist_id: int, request: Request, session: SessionDep):
    """Stop: htmx gets the buttons back once the refresh has ended (Refresh now again), with a
    note if there was nothing to stop; without htmx, the page it came from."""
    _get(session, playlist_id)
    result = await request.app.state.worker.stop_refresh(playlist_id)
    if request.headers.get("HX-Request"):
        note = None if result.outcome in ("stopped", "dropped") else result.message
        return templates.TemplateResponse(
            request,
            "partials/refresh_button.html",
            {"playlist_id": playlist_id, "state": _state(request, playlist_id), "note": note},
        )
    back = request.headers.get("referer") or f"/playlists/{playlist_id}"
    return RedirectResponse(back, status_code=303)


@router.post(
    "/playlists/{playlist_id}/tracks/{track_id}/undo-dislike",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def undo_dislike_form(playlist_id: int, track_id: int, request: Request, session: SessionDep):
    """Undo next to a "Don't like" track: htmx gets the tracks table back; without htmx, the
    playlist page."""
    playlist = _get(session, playlist_id)
    _undo_dislike(session, track_id)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "partials/tracks.html",
            {"playlist": playlist, "tracks": _tracks(session, playlist_id)},
        )
    return RedirectResponse(f"/playlists/{playlist_id}#tracks", status_code=303)


@router.get("/dont-like", response_class=HTMLResponse, include_in_schema=False)
def dont_like_page(request: Request, session: SessionDep):
    """All "Don't like" tracks of all playlists on one page."""
    return templates.TemplateResponse(
        request, "dont_like.html", {"tracks": _disliked(session), "page": "dont-like"}
    )


@router.get("/partials/dont-like", response_class=HTMLResponse, include_in_schema=False)
def dont_like_partial(request: Request, session: SessionDep):
    """The list again, reloaded by the page when a refresh ends (it may have marked tracks)."""
    return templates.TemplateResponse(
        request, "partials/dont_like_rows.html", {"tracks": _disliked(session)}
    )


@router.post("/dont-like/{track_id}/undo", response_class=HTMLResponse, include_in_schema=False)
def undo_dislike_list_form(track_id: int, request: Request, session: SessionDep):
    """Undo on the "Don't like" page: htmx gets the list back, with a note on what happens to
    the track now; without htmx, the page."""
    file, result = _undo_dislike(session, track_id)
    if request.headers.get("HX-Request"):
        track = session.get(Track, track_id)
        undone = {"title": track.title, "status": file.status} if result.ok else None
        return templates.TemplateResponse(
            request,
            "partials/dont_like_rows.html",
            {"tracks": _disliked(session), "undone": undone},
        )
    return RedirectResponse("/dont-like", status_code=303)


@router.post("/playlists/{playlist_id}/delete", include_in_schema=False)
def delete_playlist_form(playlist_id: int, request: Request, session: SessionDep):
    playlist = _get(session, playlist_id)
    if _state(request, playlist_id):
        return RedirectResponse(f"/playlists/{playlist_id}?busy=1", status_code=303)
    playlists.remove(session, playlist)
    _sync_schedule(request, playlist_id)
    return RedirectResponse("/?removed=1", status_code=303)
