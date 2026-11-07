"""Downloads: the files Releasomat downloaded into its folder, how much space they take, which
ones no playlist uses any more, and the owner's delete button for one file (phase 4).

Listing changes nothing. Deleting is **only the owner's**: a button per file leads to a
confirmation page, and only its Delete button deletes (ADR 0003). The file is deleted with
``lifecycle.delete_and_dislike``: only files Releasomat downloaded, only inside its folder, and
the track is marked "Don't like" so no refresh downloads it again (Undo on the "Don't like" page).

The JSON API under ``/api/downloads`` is documented at /docs; the pages use the same functions.
"""

from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session

from app import download_folder, files, lifecycle
from app.config import ConfigDep
from app.db import SessionDep
from app.download_folder import FolderFile
from app.models import FileStatus, TrackFile
from app.templating import templates

router = APIRouter()

NOT_IN_FOLDER = "This file isn't in Releasomat's folder now (moved, adopted or deleted already?)."


class PlaylistRef(BaseModel):
    id: int
    title: str


class DownloadedFile(BaseModel):
    id: int  # the file's id, for DELETE /api/downloads/{id}
    track_id: int
    artist: str
    title: str
    album: str | None
    path: str  # inside Releasomat's folder (MUSIC_DIR)
    size: int  # bytes
    downloaded_at: datetime
    downloader: str | None  # "slskd" (Soulseek) or "youtube"
    format: str | None
    bitrate: int | None  # kbit/s
    playlists: list[PlaylistRef]  # empty: no playlist uses the file any more
    unused: bool


class DownloadFolder(BaseModel):
    count: int
    size: int  # bytes, all files together
    unused_count: int  # files no playlist uses any more
    unused_size: int
    files: list[DownloadedFile]


class Deleted(BaseModel):
    id: int
    path: str  # the deleted file, inside Releasomat's folder
    # Playlists that still contain the track: it is "Don't like" there now, and their Navidrome
    # playlists drop the song with their next refresh.
    still_in: list[str]
    status: str  # "disliked": never downloaded again (Undo: DELETE /api/tracks/{id}/dislike)


def _as_json(f: FolderFile) -> DownloadedFile:
    return DownloadedFile(
        id=f.id,
        track_id=f.track_id,
        artist=f.artist,
        title=f.title,
        album=f.album,
        path=lifecycle.nfc(f.path),
        size=f.size,
        downloaded_at=f.downloaded_at,
        downloader=f.downloader,
        format=f.format,
        bitrate=f.bitrate,
        playlists=[PlaylistRef(id=p.id, title=p.title) for p in f.playlists],
        unused=f.unused,
    )


def _delete(session: Session, music_dir, file_id: int) -> tuple[FolderFile, TrackFile]:
    """The owner's delete of one file. 404 when it isn't in Releasomat's folder (now), 409 when
    it may not be deleted; nothing is done then."""
    found = download_folder.folder_file(session, music_dir, file_id)
    row = session.get(TrackFile, file_id)
    if found is None or row is None:
        raise HTTPException(404, NOT_IN_FOLDER)
    if row.status == FileStatus.ADOPTED or row.adopted_at:
        raise HTTPException(409, "Tagwerk adopted this file; it isn't Releasomat's now.")
    try:
        _deleted, result = lifecycle.delete_and_dislike(session, row, music_dir)
    except (files.DeleteRefused, files.UnsafePath) as exc:
        raise HTTPException(409, f"{exc} Nothing was deleted.") from None
    if not result.ok:
        # The file is gone, but the track couldn't be marked (a refresh changed it meanwhile):
        # the next refresh finds the file gone and marks it "Don't like" itself.
        raise HTTPException(409, f"The file was deleted, but: {result.reason}")
    return found, row


# --- JSON API -------------------------------------------------------------------------------


@router.get("/api/downloads", tags=["downloads"])
def downloads(session: SessionDep, config: ConfigDep, unused: bool = False) -> DownloadFolder:
    """The files Releasomat downloaded that are in its folder now, by artist and title, with
    their size and the playlists that use them. ``unused=true``: only the files no playlist
    uses any more. The totals are always for the whole folder."""
    found = download_folder.folder_files(session, config.music_dir)
    total = download_folder.summary(found)
    shown = [f for f in found if f.unused] if unused else found
    return DownloadFolder(
        count=total.count,
        size=total.size,
        unused_count=total.unused_count,
        unused_size=total.unused_size,
        files=[_as_json(f) for f in shown],
    )


@router.delete("/api/downloads/{file_id}", tags=["downloads"])
def delete_download(file_id: int, session: SessionDep, config: ConfigDep) -> Deleted:
    """**Deletes the file** (the owner's delete; can't be undone) and marks its track "Don't
    like": no refresh downloads it again. Only files Releasomat downloaded into its folder.
    404 when the file isn't there, 409 when it may not be deleted (nothing done)."""
    found, row = _delete(session, config.music_dir, file_id)
    return Deleted(
        id=file_id,
        path=lifecycle.nfc(found.path),
        still_in=[p.title for p in found.playlists],
        status=row.status,
    )


# --- Pages ----------------------------------------------------------------------------------


@router.get("/downloads", response_class=HTMLResponse, include_in_schema=False)
def downloads_page(request: Request, session: SessionDep, config: ConfigDep, unused: bool = False):
    found = download_folder.folder_files(session, config.music_dir)
    return templates.TemplateResponse(
        request,
        "downloads.html",
        {
            "page": "downloads",
            "summary": download_folder.summary(found),
            "files": [f for f in found if f.unused] if unused else found,
            "only_unused": unused,
            "deleted": request.query_params.get("deleted"),
        },
    )


@router.get("/downloads/{file_id}/delete", response_class=HTMLResponse, include_in_schema=False)
def confirm_delete_page(file_id: int, request: Request, session: SessionDep, config: ConfigDep):
    """Asks before deleting: which file, what happens to its playlists. Changes nothing."""
    found = download_folder.folder_file(session, config.music_dir, file_id)
    return templates.TemplateResponse(
        request,
        "download_delete.html",
        {"page": "downloads", "file": found, "error": None},
        status_code=200 if found else 404,
    )


@router.post("/downloads/{file_id}/delete", include_in_schema=False)
def delete_form(file_id: int, request: Request, session: SessionDep, config: ConfigDep):
    """The Delete button on the confirmation page."""
    try:
        found, _row = _delete(session, config.music_dir, file_id)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "download_delete.html",
            {
                "page": "downloads",
                "file": download_folder.folder_file(session, config.music_dir, file_id),
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    back = "/downloads?" + _query(deleted=found.title, unused=request.query_params.get("unused"))
    return RedirectResponse(back, status_code=303)


def _query(**values) -> str:
    """A query string with only the values that are set."""
    return urlencode({k: v for k, v in values.items() if v})
