"""The integration API for Tagwerk, the owner's library manager (ADR 0009), under
``/api/integration/``.

**It is optional.** Releasomat works in full without Tagwerk: refreshes, downloads, playlists and
the handling of files that leave its folder never wait for Tagwerk or call it. Only Tagwerk calls
Releasomat, never the other way round.

- **Off** until the owner sets a key (Settings → Tagwerk, ``INTEGRATION_API_KEY``): every call
  answers 403 ``integration_off``. With a key, Tagwerk sends it as ``Authorization: Bearer
  <key>``; a missing or wrong key answers 401 ``wrong_key``. The key is checked before anything
  else (no key never gives 422), compared in constant time, and never logged or echoed.
- Tagwerk names files by their **path inside Releasomat's folder** (``/``, Unicode NFC, e.g.
  ``Röyksopp/Röyksopp - Eple.opus``; ``Releasomat/…`` and ``/music/Releasomat/…`` work too) plus
  their size. Answers carry Releasomat's file ``id``, which later notices may send along.
- Every ``POST`` takes ``{"files": [...]}`` and answers per file, in the order sent: ``ok``, or a
  plain-language ``reason``. Repeating a call that went through answers ``ok`` again.
- Everything here changes only the database (``app/lifecycle.py``), except ``delete``: the owner's
  delete, started on Tagwerk's Releasomat page, which deletes the file with Releasomat's own
  owner's delete (``lifecycle.delete_and_dislike``; ADR 0003: only files Releasomat downloaded,
  only inside its folder).
- Errors share one shape: ``{"integration_api": 1, "error": "<code>", "message": "<plain words>"}``.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.utils import get_authorization_scheme_param
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__, files, lifecycle
from app import settings as user_settings
from app.config import ConfigDep, get_config
from app.db import SessionDep, get_engine
from app.lifecycle import Result
from app.models import DislikeReason, FileStatus, Playlist, PlaylistTrack, Track, TrackFile, utcnow

API_VERSION = 1  # goes up only for changes an older Tagwerk can't handle
PREFIX = "/api/integration"
KEY = "INTEGRATION_API_KEY"

OFF = (
    "The integration is off: Releasomat has no key for Tagwerk (Settings → Tagwerk). "
    "Releasomat works the same without it."
)
WRONG_KEY = "The key is missing or wrong. Send Releasomat's key as 'Authorization: Bearer …'."
UNKNOWN_FILE = "Releasomat doesn't know a file at this path."
SIZE_DIFFERS = (
    "The file at this path has another size than the one sent: it may be a different file now. "
    "Nothing was changed."
)


# --- Errors: one shape for every answer that isn't a success ---------------------------------


class IntegrationError(Exception):
    """An answer like ``{"integration_api": 1, "error": "wrong_key", "message": "…"}``."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ErrorAnswer(BaseModel):
    integration_api: int = API_VERSION
    error: str = Field(description="integration_off, wrong_key, bad_request, not_found, …")
    message: str = Field(description="What went wrong, in plain words.")


def error_answer(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    if status == 401:
        headers = {**(headers or {}), "WWW-Authenticate": "Bearer"}
    body = ErrorAnswer(error=code, message=message).model_dump()
    return JSONResponse(body, status_code=status, headers=headers)


def is_integration(request: Request) -> bool:
    path = request.url.path
    return path == PREFIX or path.startswith(f"{PREFIX}/")


async def integration_error(request: Request, exc: IntegrationError) -> JSONResponse:
    return error_answer(exc.status, exc.code, exc.message)


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """A body that doesn't fit. For the integration API the key is checked first (so a call
    without the key gets 401 or 403, never 422) and the answer has the integration's shape;
    every other route keeps FastAPI's own answer."""
    if not is_integration(request):
        return await request_validation_exception_handler(request, exc)
    scheme, sent = get_authorization_scheme_param(request.headers.get("Authorization"))
    try:
        with Session(get_engine(get_config().database_url)) as session:
            check_key(session, sent if scheme.lower() == "bearer" else None)
    except IntegrationError as error:
        return await integration_error(request, error)
    problems = []
    for problem in exc.errors()[:3]:
        where = ".".join(str(part) for part in problem.get("loc", ()) if part != "body")
        problems.append(f"{where}: {problem.get('msg')}" if where else str(problem.get("msg")))
    return error_answer(422, "bad_request", "The request doesn't fit: " + "; ".join(problems))


async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """An unknown call or method under /api/integration/ in the integration's shape; every other
    route keeps FastAPI's own answer."""
    if not is_integration(request):
        return await http_exception_handler(request, exc)
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
    return error_answer(exc.status_code, code, str(exc.detail), exc.headers)


def add_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(IntegrationError, integration_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(StarletteHTTPException, http_error)


# --- The key --------------------------------------------------------------------------------


def current_key(session: Session) -> str | None:
    """The key Tagwerk must send, or None while the integration is off: no key, or one that
    breaks the setting's rules (an env var shorter than 16 characters, with spaces, …)."""
    value = user_settings.get(session, KEY)
    try:
        return user_settings.clean(user_settings.BY_KEY[KEY], value) or None
    except user_settings.InvalidSetting:
        return None


def check_key(session: Session, sent: str | None) -> None:
    """Raises IntegrationError when the integration is off (403) or ``sent`` isn't the key
    (401). Compared in constant time, so the answer's timing gives nothing away."""
    key = current_key(session)
    if key is None:
        raise IntegrationError(403, "integration_off", OFF)
    if not sent or not secrets.compare_digest(sent.encode(), key.encode()):
        raise IntegrationError(401, "wrong_key", WRONG_KEY)


@dataclass(frozen=True)
class LastCall:
    """Tagwerk's latest call with the right key, for the Settings page. Kept in memory only."""

    at: datetime
    call: str  # "info", "files", "adopt", …


def last_call(app: FastAPI) -> LastCall | None:
    return getattr(app.state, "integration_last_call", None)


bearer = HTTPBearer(
    auto_error=False,
    scheme_name="TagwerkKey",
    description="Releasomat's key for Tagwerk (Settings → Tagwerk), sent as "
    "`Authorization: Bearer <key>`.",
)


def integration_key(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    """Runs before every call (a router dependency, so before the body is checked)."""
    check_key(session, credentials.credentials if credentials else None)
    call = request.url.path.removeprefix(f"{PREFIX}/")
    request.app.state.integration_last_call = LastCall(utcnow(), call)


ERRORS = {
    401: {"model": ErrorAnswer, "description": "The key is missing or wrong (`wrong_key`)."},
    403: {"model": ErrorAnswer, "description": "No key is set: the integration is off."},
    422: {"model": ErrorAnswer, "description": "The request doesn't fit (`bad_request`)."},
}

router = APIRouter(
    prefix=PREFIX,
    tags=["integration"],
    dependencies=[Depends(integration_key)],
    responses=ERRORS,
)


# --- What Tagwerk sends and gets back ---------------------------------------------------------


class Info(BaseModel):
    app: str = "releasomat"
    version: str
    integration_api: int = API_VERSION
    folder: str = Field(description="Releasomat's folder inside the library (NAVIDROME_SUBFOLDER).")


class Wanted(BaseModel):
    """The track the file was downloaded for, as the playlist's source lists it. BPM and key
    only when the source has them (Beatport, Tidal)."""

    artist: str
    title: str
    album: str | None = None
    duration: float | None = Field(default=None, description="In seconds.")
    isrc: str | None = None
    mbid: str | None = Field(default=None, description="MusicBrainz recording ID.")
    bpm: float | None = None
    key: str | None = Field(default=None, description="As the source writes it, e.g. 'Gb Minor'.")


class PlaylistLink(BaseModel):
    id: int
    name: str
    url: str = Field(description="The playlist's page in Releasomat.")


class AreaFile(BaseModel):
    id: int = Field(description="Releasomat's file id; later notices may send it along.")
    path: str = Field(description="Inside Releasomat's folder, with '/', Unicode NFC.")
    size: int = Field(description="In bytes.")
    downloaded_at: datetime | None = None
    downloader: str | None = Field(default=None, description="slskd or youtube.")
    format: str | None = None
    bitrate: int | None = Field(default=None, description="In kbit/s.")
    wanted: Wanted
    playlists: list[PlaylistLink] = Field(description="Releasomat playlists with this track.")


class AreaFiles(BaseModel):
    integration_api: int = API_VERSION
    folder: str
    files: list[AreaFile]


class FileRef(BaseModel):
    """A file in Releasomat's folder, as Tagwerk names it."""

    path: str = Field(
        description="Inside Releasomat's folder ('Röyksopp/Röyksopp - Eple.opus'); "
        "'Releasomat/…' and '/music/Releasomat/…' work too.",
    )
    size: int | None = Field(
        default=None,
        ge=0,
        description="In bytes. adopt and delete refuse a file that differs; delete needs it.",
    )
    id: int | None = Field(default=None, description="Releasomat's file id from an earlier answer.")


class FileList(BaseModel):
    files: list[FileRef]


class ReturnedFiles(FileList):
    why: Literal["move_failed", "given_back"] = Field(
        description="move_failed: Tagwerk's move failed, the file never left. given_back: the "
        "owner gave an adopted file back into Releasomat's folder."
    )


class FileAnswer(BaseModel):
    asked: str = Field(description="The path as it was sent.")
    ok: bool
    changed: bool = Field(
        default=False, description="False when the file already was so (a repeated call)."
    )
    reason: str | None = Field(default=None, description="Why not, in plain words.")
    id: int | None = None
    path: str | None = Field(
        default=None, description="Where the file is, or was, in Releasomat's folder."
    )
    status: str | None = Field(
        default=None, description="Releasomat's state of the file now: done, adopted, disliked, …"
    )


class FileAnswers(BaseModel):
    integration_api: int = API_VERSION
    files: list[FileAnswer]


# --- The calls ------------------------------------------------------------------------------


@router.get("/info")
def info(session: SessionDep) -> Info:
    """Who answers, and which folder inside the library is Releasomat's."""
    return Info(version=__version__, folder=_folder(session))


@router.get("/files")
def area_files(request: Request, session: SessionDep, config: ConfigDep) -> AreaFiles:
    """The files in Releasomat's folder now: downloaded by Releasomat, still its own, and on
    disk. Files that left (adopted, deleted, moved) are not listed."""
    rows = session.exec(
        select(TrackFile, Track)
        .join(Track, Track.id == TrackFile.track_id)
        .where(TrackFile.downloaded_by_releasomat.is_(True), TrackFile.path.is_not(None))
        .order_by(TrackFile.path)
    ).all()
    playlists = _playlists_of(request, session, [track.id for _, track in rows])
    found = []
    for file, track in rows:
        on_disk = _stat(config.music_dir, file.path)
        if on_disk is None:
            continue
        found.append(
            AreaFile(
                id=file.id,
                path=lifecycle.nfc(file.path),
                size=on_disk.st_size,
                downloaded_at=_downloaded_at(file, on_disk.st_mtime),
                downloader=file.downloader,
                format=file.format,
                bitrate=file.bitrate,
                wanted=Wanted(
                    artist=track.artist,
                    title=track.title,
                    album=track.album,
                    duration=track.duration,
                    isrc=track.isrc,
                    mbid=track.mbid,
                    bpm=track.bpm,
                    key=track.key,
                ),
                playlists=playlists.get(track.id, []),
            )
        )
    return AreaFiles(folder=_folder(session), files=found)


@router.post("/adopt")
def adopt(body: FileList, session: SessionDep, config: ConfigDep) -> FileAnswers:
    """The owner clicked Adopt in Tagwerk, which moves the files into its inbox next. From now
    on Releasomat leaves them alone and never downloads their tracks again; it links each song
    once it is in the library. Only files Releasomat downloaded into its folder."""

    def act(session: Session, file: TrackFile, ref: FileRef) -> Result:
        if _size_differs(config.music_dir, file, ref.size):
            return Result(False, reason=SIZE_DIFFERS)
        return lifecycle.adopt(session, file)

    return _each(session, body.files, act)


@router.post("/returned")
def returned(body: ReturnedFiles, session: SessionDep) -> FileAnswers:
    """Adopted files that are back in Releasomat's folder at their old path: Tagwerk's move
    failed, or the owner gave them back. They are normal downloads again."""

    def act(session: Session, file: TrackFile, ref: FileRef) -> Result:
        return lifecycle.returned(session, file, body.why)

    return _each(session, body.files, act)


@router.post("/discarded")
def discarded(body: FileList, session: SessionDep) -> FileAnswers:
    """The owner threw adopted files away in Tagwerk's inbox: "Don't like", never downloaded
    again (a library copy stays linked)."""
    return _each(session, body.files, lambda s, file, ref: lifecycle.discard(s, file))


@router.post("/restored")
def restored(body: FileList, session: SessionDep) -> FileAnswers:
    """Undo of ``discarded``: the owner took the files back out of Tagwerk's inbox trash, so
    they are adopted again."""
    return _each(session, body.files, lambda s, file, ref: lifecycle.restore(s, file))


@router.post("/delete")
def delete(body: FileList, session: SessionDep, config: ConfigDep) -> FileAnswers:
    """The owner pressed Delete on Tagwerk's Releasomat page: Releasomat deletes each file with
    its own owner's delete (only files it downloaded, only inside its folder; ADR 0003) and marks
    the track "Don't like". **The only call that touches a file.**"""

    def act(session: Session, file: TrackFile, ref: FileRef) -> Result:
        if file.status == FileStatus.ADOPTED or file.adopted_at:
            return Result(False, reason="Tagwerk adopted this file; it isn't Releasomat's now.")
        if not file.path and (file.status == FileStatus.DISLIKED or file.disliked_at):
            # Gone already and "Don't like" (also after a refresh found the file gone: the
            # owner's word "deleted" replaces that guess). Nothing to delete.
            return lifecycle.mark_disliked(session, file, DislikeReason.DELETED)
        if not file.path or not file.downloaded_by_releasomat:
            return Result(False, reason="This file isn't in Releasomat's folder now.")
        if _stat(config.music_dir, file.path) is None:
            return Result(False, reason="The file isn't there (moved or deleted already?).")
        if ref.size is None:  # deleting can't be undone: only the exact file Tagwerk showed
            return Result(False, reason="Send the file's size with delete. Nothing was deleted.")
        if _size_differs(config.music_dir, file, ref.size):
            return Result(False, reason=SIZE_DIFFERS)
        try:
            _deleted, result = lifecycle.delete_and_dislike(session, file, config.music_dir)
        except (files.DeleteRefused, files.UnsafePath) as exc:
            return Result(False, reason=f"{exc} Nothing was deleted.")
        return result

    return _each(session, body.files, act)


# --- Helpers --------------------------------------------------------------------------------


def _folder(session: Session) -> str:
    return user_settings.get(session, "NAVIDROME_SUBFOLDER")


def _each(session: Session, refs: list[FileRef], act) -> FileAnswers:
    """Find each file by its path (and id, if sent) and ``act`` on it; one answer per file."""
    folder = _folder(session)
    answers = []
    for ref in refs:
        try:
            file = lifecycle.find_by_path(session, ref.path, folder, file_id=ref.id)
        except (lifecycle.BadPath, lifecycle.AmbiguousPath) as exc:
            answers.append(FileAnswer(asked=ref.path, ok=False, reason=str(exc)))
            continue
        if file is None:
            answers.append(FileAnswer(asked=ref.path, ok=False, reason=UNKNOWN_FILE))
            continue
        result = act(session, file, ref)
        answers.append(
            FileAnswer(
                asked=ref.path,
                ok=result.ok,
                changed=result.changed,
                reason=result.reason,
                id=file.id,
                path=lifecycle.nfc(file.path or file.former_path),
                status=file.status,
            )
        )
    return FileAnswers(files=answers)


def _stat(music_dir: Path, path: str):
    """The file's size and time if it is a plain file inside Releasomat's folder, else None.
    Only looks; changes nothing."""
    try:
        target = files.inside(music_dir, path)
        if target.is_symlink() or not target.is_file():
            return None
        return target.stat()
    except (files.UnsafePath, OSError):
        return None


def _size_differs(music_dir: Path, file: TrackFile, size: int | None) -> bool:
    """True when Tagwerk sent a size and the file in Releasomat's folder has another one."""
    if size is None or not file.path:
        return False
    on_disk = _stat(music_dir, file.path)
    return on_disk is not None and on_disk.st_size != size


def _downloaded_at(file: TrackFile, mtime: float) -> datetime:
    """When the download was saved (kept since 2026-10; for older files the file's time)."""
    stored = (file.details or {}).get("downloaded_at")
    if stored:
        try:
            return datetime.fromisoformat(stored)
        except ValueError:
            pass
    return datetime.fromtimestamp(mtime, UTC)


def _playlists_of(
    request: Request, session: Session, track_ids: list[int]
) -> dict[int, list[PlaylistLink]]:
    """The playlists that contain each track now (one query)."""
    if not track_ids:
        return {}
    rows = session.exec(
        select(PlaylistTrack.track_id, Playlist.id, Playlist.title)
        .join(Playlist, Playlist.id == PlaylistTrack.playlist_id)
        .where(PlaylistTrack.track_id.in_(track_ids), PlaylistTrack.removed_at.is_(None))
        .order_by(Playlist.title)
    ).all()
    found: dict[int, list[PlaylistLink]] = {}
    for track_id, playlist_id, title in rows:
        url = str(request.url_for("playlist_page", playlist_id=playlist_id))
        links = found.setdefault(track_id, [])
        if all(link.id != playlist_id for link in links):
            links.append(PlaylistLink(id=playlist_id, name=title, url=url))
    return found


# --- For the Settings page ------------------------------------------------------------------


def overview(request: Request, session: Session) -> dict:
    """What the Settings page shows in the Tagwerk group's status box."""
    address = str(request.base_url).rstrip("/")
    return {
        "on": current_key(session) is not None,
        "set": bool(user_settings.get(session, KEY)),
        "locked": user_settings.is_locked(KEY),
        "address": address,
        "local": request.base_url.hostname in ("localhost", "127.0.0.1", "::1"),
        "folder": _folder(session),
        "last_call": last_call(request.app),
    }
