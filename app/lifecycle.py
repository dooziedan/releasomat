"""What happens to a downloaded file after the download: every change of a ``track_file`` row's
state, in one place (ADR 0009, ``docs/plans/tagwerk-integration-releasomat.md`` §2).

Releasomat's folder is a *discovery area*. Files leave it only by the owner's action:

- **adopted**: the owner clicked Adopt in Tagwerk, which moves the file into its import inbox.
  Releasomat never downloads the track again. Each refresh looks for the song in Navidrome
  outside Releasomat's folder and links it once it is in the library.
- **disliked** ("Don't like"): the owner deleted the file (in Releasomat, with Tagwerk's Delete,
  or by throwing the adopted file away in Tagwerk's inbox), or it left the folder and isn't
  anywhere else in the library. Never downloaded again, but linked if the song is in the
  library. **Undo** makes it a normal missing track again.

The status says where the song is *now* (the badge); the time stamps ``adopted_at`` and
``disliked_at`` remember what happened. So an adopted track whose library copy is linked is
``in_library`` with ``adopted_at`` set, and when that song disappears from Navidrome it goes
back to ``adopted`` (``status_after_song_gone``).

Everything here works on the database only and never on files, with one exception:
``delete_and_dislike`` (the owner's delete), which deletes through ``files.delete_by_owner``.

**Conditional updates.** Every change is one ``UPDATE … WHERE`` that only acts if the row is
still in the state the decision was made on ("only if still ``done`` at this path"). When two
callers act at the same moment (Tagwerk's adopt notice and a refresh that sees the file gone),
only one changes the row; the other one looks again and answers from the new state. Every
function commits and reloads ``track_file``, which must be a row loaded with ``session``.

**Answers.** Each state change returns a ``Result``: ``ok`` (the row is now in the asked state),
``changed`` (this call changed it; False when it already was, so repeating a call answers ok
again) and, when not ok, a plain-language ``reason``.
"""

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, update
from sqlmodel import Session, select

from app import files
from app.models import DislikeReason, FileStatus, TrackFile, utcnow

ADOPTED = FileStatus.ADOPTED.value
DISLIKED = FileStatus.DISLIKED.value
DONE = FileStatus.DONE.value
IN_LIBRARY = FileStatus.IN_LIBRARY.value
PENDING = FileStatus.PENDING.value
VANISHED = DislikeReason.VANISHED.value
DISCARDED = DislikeReason.DISCARDED.value

# Tracks Releasomat never downloads (again): Tagwerk has the file, or the owner doesn't want it.
KEPT_AWAY = (ADOPTED, DISLIKED)
RETURN_REASONS = ("move_failed", "given_back")
TRIES = 3  # how often a change is decided again when another caller changed the row first

CHANGED_MEANWHILE = "The file's state changed at the same moment; try again."
NOT_ADOPTED = "Releasomat doesn't know this file as adopted."
OWNER_DISLIKED = "The owner marked this track “Don't like”."


@dataclass(frozen=True)
class Result:
    """The answer of a state change (see the module docstring)."""

    ok: bool
    changed: bool = False
    reason: str | None = None


class BadPath(ValueError):
    """A path Releasomat refuses to look up: empty, with NUL, ``..``, ``.`` or empty parts."""


class AmbiguousPath(Exception):
    """More than one track file matches a path. Releasomat never guesses which one is meant."""

    def __init__(self, path: str, file_ids: list[int]):
        ids = ", ".join(str(i) for i in file_ids)
        super().__init__(f"{path!r} matches several files ({ids}); name the file by its id.")
        self.file_ids = file_ids


# --- Finding a file by the path Tagwerk sends -----------------------------------------------


def nfc(text: str | None) -> str | None:
    return unicodedata.normalize("NFC", text) if text is not None else None


def area_paths(raw: str, subfolder: str) -> list[str]:
    """The paths inside Releasomat's folder (relative to it, ``/``, NFC) that ``raw`` may mean:

    - ``"Röyksopp/Röyksopp - Eple.opus"`` as given;
    - ``"Releasomat/Röyksopp/…"``: also without the leading folder name (``subfolder``);
    - ``"/music/Releasomat/Röyksopp/…"``: the part after the folder name (for each place it
      appears). Other absolute paths mean nothing.

    Raises BadPath for an empty path, NUL, and ``..``, ``.`` or empty parts."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise BadPath("The path is empty or invalid.")
    text = nfc(raw)
    absolute = text.startswith("/")
    parts = text.split("/")[1:] if absolute else text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BadPath(f"{raw!r} is not a plain path (no '..', '.' or empty parts).")
    folder = [p for p in (nfc(subfolder) or "").split("/") if p]
    size = len(folder)
    candidates = []
    if absolute:
        for start in range(len(parts) - size):
            if folder and parts[start : start + size] == folder:
                candidates.append("/".join(parts[start + size :]))
    else:
        candidates.append(text)
        if folder and len(parts) > size and parts[:size] == folder:
            candidates.append("/".join(parts[size:]))
    return list(dict.fromkeys(candidates))


def find_by_path(
    session: Session, raw: str, subfolder: str, *, file_id: int | None = None
) -> TrackFile | None:
    """The track file a path inside Releasomat's folder names (see ``area_paths``), matched
    against where the file is (``path``) and where it was before it left (``former_path``),
    both compared in Unicode NFC. With ``file_id`` (Tagwerk's later notices carry it) only that
    row is considered.

    Returns None when nothing matches. Raises BadPath for a refused path and AmbiguousPath when
    several rows match: Releasomat never guesses."""
    wanted = set(area_paths(raw, subfolder))
    if not wanted:
        return None
    query = select(TrackFile.id, TrackFile.path, TrackFile.former_path).where(
        or_(TrackFile.path.is_not(None), TrackFile.former_path.is_not(None))
    )
    if file_id is not None:
        query = query.where(TrackFile.id == file_id)
    matches = sorted(
        {
            row_id
            for row_id, path, former in session.exec(query).all()
            if nfc(path) in wanted or nfc(former) in wanted
        }
    )
    if not matches:
        return None
    if len(matches) > 1:
        raise AmbiguousPath(raw, matches)
    return session.get(TrackFile, matches[0])


# --- The changes ----------------------------------------------------------------------------


def _update(session: Session, track_file: TrackFile, where: dict, values: dict) -> bool:
    """Change the row only if it still has the values in ``where`` (one conditional UPDATE).
    Commits and reloads ``track_file``. True when this call changed it."""
    conditions = [TrackFile.id == track_file.id]
    for column, value in where.items():
        attribute = getattr(TrackFile, column)
        conditions.append(attribute.is_(None) if value is None else attribute == value)
    statement = (
        update(TrackFile)
        .where(*conditions)
        .values(**values, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    changed = session.exec(statement).rowcount == 1
    session.commit()
    session.refresh(track_file)
    return changed


def _details(track_file: TrackFile, **values) -> dict:
    return {**(track_file.details or {}), **values}


def _old_song_id(track_file: TrackFile) -> str | None:
    """The Navidrome ID of the file in Releasomat's folder, kept when the file leaves: Navidrome
    lists that song until its next scan, so a refresh only links it again once Navidrome says
    it is elsewhere in the library (it keeps the ID when it notices a move)."""
    return track_file.navidrome_song_id or (track_file.details or {}).get("old_song_id")


def _song_values(song: dict) -> dict:
    return {
        "navidrome_song_id": song["id"],
        "format": song.get("suffix"),
        "bitrate": song.get("bitRate"),
        "downloaded_by_releasomat": False,
        "error": None,
    }


def adopt(session: Session, track_file: TrackFile) -> Result:
    """Tagwerk is about to take the file into its inbox (the owner clicked Adopt). From now on
    Releasomat leaves the path alone: the file disappearing is expected, and the track is never
    downloaded again. Only a file Releasomat downloaded into its folder can be adopted.

    A late notice for a file a refresh already guessed was gone ("Don't like", ``vanished``)
    still counts; one for a file the owner moved into the library just records the adoption."""
    for _ in range(TRIES):
        f = track_file
        if f.status == ADOPTED or (f.adopted_at and f.status == IN_LIBRARY):
            return Result(True)
        if f.status == DONE and f.downloaded_by_releasomat and f.path:
            where = {"status": DONE, "path": f.path, "downloaded_by_releasomat": True}
            values = {
                "status": ADOPTED,
                "former_path": f.path,
                "path": None,
                "downloaded_by_releasomat": False,
                "navidrome_song_id": None,
                "adopted_at": utcnow(),
                "error": None,
                "details": _details(f, old_song_id=_old_song_id(f)),
            }
        elif f.status == DISLIKED and f.disliked_reason == VANISHED and f.former_path:
            where = {"status": DISLIKED, "disliked_reason": VANISHED}
            values = {
                "status": ADOPTED,
                "adopted_at": utcnow(),
                "disliked_at": None,
                "disliked_reason": None,
            }
        elif f.status == IN_LIBRARY and f.former_path and (f.details or {}).get("moved"):
            where = {"status": IN_LIBRARY, "adopted_at": None}
            values = {"adopted_at": utcnow()}
        elif f.status == DISLIKED:
            return Result(False, reason=OWNER_DISLIKED)
        else:
            return Result(False, reason="Releasomat has no downloaded file at this path.")
        if _update(session, f, where, values):
            return Result(True, True)
    return Result(False, reason=CHANGED_MEANWHILE)


def returned(session: Session, track_file: TrackFile, why: str) -> Result:
    """The adopted file is back in Releasomat's folder, at its old path: Tagwerk's move failed
    (``why="move_failed"``, the file never left) or the owner gave it back (``"given_back"``).
    The track is a normal download again, exactly as before ``adopt``."""
    if why not in RETURN_REASONS:
        raise ValueError(f"why must be one of {', '.join(RETURN_REASONS)}.")
    for _ in range(TRIES):
        f = track_file
        if f.status == DONE and f.downloaded_by_releasomat and f.path and not f.adopted_at:
            return Result(True)
        returnable = f.status == ADOPTED or (f.status == IN_LIBRARY and f.adopted_at)
        returnable = returnable or (f.status == DISLIKED and f.disliked_reason == VANISHED)
        if not f.former_path or not returnable:
            if f.status == DISLIKED:
                return Result(False, reason=OWNER_DISLIKED)
            return Result(False, reason=NOT_ADOPTED)
        # After a failed move Navidrome still has the song under its old ID; a file that was
        # away is found again after the next scan.
        old_id = (f.details or {}).get("old_song_id") if why == "move_failed" else None
        values = {
            "status": DONE,
            "path": f.former_path,
            "former_path": None,
            "downloaded_by_releasomat": True,
            "navidrome_song_id": old_id,
            "adopted_at": None,
            "disliked_at": None,
            "disliked_reason": None,
            "error": None,
            "details": _details(f, returned=why),
        }
        where = {"status": f.status, "former_path": f.former_path}
        if _update(session, f, where, values):
            return Result(True, True)
    return Result(False, reason=CHANGED_MEANWHILE)


def discard(session: Session, track_file: TrackFile) -> Result:
    """The owner threw the adopted file away in Tagwerk's inbox: "Don't like" (``discarded``).
    A track whose library copy is linked stays linked."""
    for _ in range(TRIES):
        f = track_file
        if f.disliked_at and f.disliked_reason != VANISHED:
            return Result(True)  # already "Don't like"
        if f.status == DISLIKED:  # a guessed "Don't like": the owner's word replaces it
            where = {"status": DISLIKED, "disliked_reason": f.disliked_reason}
            values = {"disliked_reason": DISCARDED, "disliked_at": utcnow()}
        elif f.status == ADOPTED or (f.status == IN_LIBRARY and f.adopted_at):
            where = {"status": f.status, "disliked_at": None}
            values = {"disliked_at": utcnow(), "disliked_reason": DISCARDED}
            if f.status == ADOPTED:
                values["status"] = DISLIKED
        else:
            return Result(False, reason=NOT_ADOPTED)
        if _update(session, f, where, values):
            return Result(True, True)
    return Result(False, reason=CHANGED_MEANWHILE)


def restore(session: Session, track_file: TrackFile) -> Result:
    """Undo of ``discard``: the owner took the file back out of Tagwerk's inbox trash, so the
    track is adopted again (or stays linked to its library copy)."""
    for _ in range(TRIES):
        f = track_file
        if f.disliked_reason == DISCARDED and f.status in (DISLIKED, IN_LIBRARY):
            where = {"status": f.status, "disliked_reason": DISCARDED}
            values = {"disliked_at": None, "disliked_reason": None}
            if f.status == DISLIKED:
                values |= {"status": ADOPTED, "adopted_at": f.adopted_at or utcnow()}
        elif not f.disliked_at and (f.status == ADOPTED or f.adopted_at):
            return Result(True)  # already back (adopted, or its library copy linked)
        elif f.status == DISLIKED:
            return Result(False, reason=OWNER_DISLIKED)
        else:
            return Result(False, reason="This file wasn't thrown away in Tagwerk.")
        if _update(session, f, where, values):
            return Result(True, True)
    return Result(False, reason=CHANGED_MEANWHILE)


def mark_disliked(session: Session, track_file: TrackFile, reason: str) -> Result:
    """Mark a track "Don't like" (a ``DislikeReason``): never downloaded again; Navidrome's
    copy in Releasomat's folder is no longer linked (a library copy is linked again by the next
    refresh, if there is one). A file still in Releasomat's folder must be deleted first
    (``delete_and_dislike``), so it is never left behind without its row: only ``deleted`` and
    ``vanished`` are accepted for a row with a path."""
    reason = DislikeReason(reason).value
    for _ in range(TRIES):
        f = track_file
        if f.status == DISLIKED or (f.status == IN_LIBRARY and f.disliked_at):
            if f.disliked_reason != VANISHED or reason == VANISHED:
                return Result(True)
            where = {"status": f.status, "disliked_reason": VANISHED}
            values = {"disliked_reason": reason, "disliked_at": utcnow()}
        else:
            if f.path and reason not in (DislikeReason.DELETED, VANISHED):
                return Result(
                    False, reason="The track's file is still in Releasomat's folder; delete it."
                )
            where = {"status": f.status, "path": f.path}
            values = {
                "status": DISLIKED,
                "former_path": f.path or f.former_path,
                "path": None,
                "downloaded_by_releasomat": False,
                "navidrome_song_id": None,
                "disliked_at": utcnow(),
                "disliked_reason": reason,
                "error": None,
            }
            if f.downloaded_by_releasomat:  # Navidrome's copy in the folder: never link it again
                values["details"] = _details(f, old_song_id=_old_song_id(f))
        if _update(session, f, where, values):
            return Result(True, True)
    return Result(False, reason=CHANGED_MEANWHILE)


def undo_dislike(session: Session, track_file: TrackFile) -> Result:
    """Undo "Don't like" (the playlist page): the track is a normal missing track again and the
    next refresh downloads it (a linked library copy stays linked). It no longer counts as
    adopted either."""
    for _ in range(TRIES):
        f = track_file
        if not f.disliked_at and f.status != DISLIKED:
            return Result(True)
        where = {"status": f.status, "disliked_reason": f.disliked_reason}
        values = {"disliked_at": None, "disliked_reason": None, "adopted_at": None, "error": None}
        if f.status == DISLIKED:
            values["status"] = PENDING
        if _update(session, f, where, values):
            return Result(True, True)
    return Result(False, reason=CHANGED_MEANWHILE)


def left_folder(
    session: Session,
    track_file: TrackFile,
    path: str,
    song: dict | None = None,
    *,
    matched_by: str | None = None,
    score: float | None = None,
) -> Result:
    """A refresh found that a downloaded file left Releasomat's folder without an adoption
    notice. ``song``: its copy elsewhere in the library (the owner moved it there), which is
    linked from now on; None: not found anywhere, so "Don't like" (``vanished``), lifted by
    ``link_library_copy`` if the song turns up later.

    Only acts while the row is still ``done`` at ``path``: an adopt notice that came first
    wins (``ok`` False, ``changed`` False)."""
    f = track_file
    where = {"status": DONE, "path": path, "downloaded_by_releasomat": True}
    values = {
        "former_path": path,
        "path": None,
        "downloaded_by_releasomat": False,
        "navidrome_song_id": None,
        "error": None,
    }
    if song is not None:
        values |= _song_values(song) | {"status": IN_LIBRARY}
        details = {"moved": True, "matched_by": matched_by, "score": score}
    else:
        values |= {"status": DISLIKED, "disliked_at": utcnow(), "disliked_reason": VANISHED}
        details = {}
    values["details"] = _details(f, old_song_id=_old_song_id(f), **details)
    if _update(session, f, where, values):
        return Result(True, True)
    return Result(False, reason=CHANGED_MEANWHILE)


def link_library_copy(
    session: Session,
    track_file: TrackFile,
    song: dict,
    *,
    matched_by: str | None = None,
    score: float | None = None,
) -> Result:
    """The song of an adopted or "Don't like" track was found elsewhere in the library: link
    it (the Navidrome playlist uses the library copy). The time stamps stay; a guessed
    "Don't like" (``vanished``) is lifted, since the song turned up after all."""
    for _ in range(TRIES):
        f = track_file
        if f.status not in KEPT_AWAY:
            return Result(False, reason="Only adopted and “Don't like” tracks.")
        values = _song_values(song) | {"status": IN_LIBRARY}
        details = {"matched_by": matched_by, "score": score}
        if f.disliked_reason == VANISHED:
            values |= {"disliked_at": None, "disliked_reason": None}
            details["moved"] = True
        values["details"] = _details(f, **details)
        if _update(session, f, {"status": f.status}, values):
            return Result(True, True)
    return Result(False, reason=CHANGED_MEANWHILE)


def status_after_song_gone(track_file: TrackFile) -> str:
    """The status of a linked track whose song is no longer in Navidrome: "Don't like" and
    adopted tracks keep being kept away; every other track is missing again."""
    if track_file.disliked_at:
        return DISLIKED
    if track_file.adopted_at:
        return ADOPTED
    return PENDING


def delete_and_dislike(
    session: Session, track_file: TrackFile, music_dir: Path
) -> tuple[files.OwnerDelete | None, Result]:
    """The owner's delete (Tagwerk's Delete button, later Releasomat's own): delete the file
    with ``files.delete_by_owner`` (only files Releasomat downloaded, only in its folder; ADR
    0003), then mark the track "Don't like" (``deleted``). Raises DeleteRefused / UnsafePath
    (nothing done) when the file may not be deleted. Repeating it after it went through answers
    ok without touching anything (``None`` instead of what was deleted)."""
    session.refresh(track_file)
    f = track_file
    if f.status == DISLIKED and f.disliked_reason == DislikeReason.DELETED and not f.path:
        return None, Result(True)
    deleted = files.delete_by_owner(session, f, music_dir)
    return deleted, mark_disliked(session, f, DislikeReason.DELETED)
