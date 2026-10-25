"""The only place that creates, moves or deletes music files (ADR 0003).

Losing or overwriting music is the worst thing Releasomat could do, so every file operation goes
through this module and follows three rules:

1. **Stay inside.** Every path is resolved (symlinks followed) and checked to be inside its folder:
   MUSIC_DIR for everything Releasomat writes, the staging folder or SLSKD_DOWNLOADS_DIR for the
   downloads it moves from. ``..`` in a path is refused outright, so is an absolute path or a
   symlink that leads elsewhere. See ``inside()``.
2. **Never overwrite.** If a file name is taken, the new file gets " (2)", " (3)", … before its
   extension. The check and the creation are one step (``open(…, "xb")``), so even two downloads
   finishing at the same moment can't overwrite each other. A new file in MUSIC_DIR appears in
   one step: it is copied under a hidden name and renamed when complete (``save_download``).
3. **Never delete on its own.** No refresh, schedule, playlist change or setting deletes music.
   Only the owner does, with a delete button for one file (``delete_by_owner()``, phase 4), and
   only for a file Releasomat downloaded itself. Songs that were already in Navidrome are linked,
   never touched.

Downloads first land in a staging folder inside CONFIG_DIR (``new_staging_dir()``); only
``save_download()`` brings them into MUSIC_DIR.
"""

import os
import shutil
import string
import tempfile
import unicodedata
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path

from sqlmodel import Session, select

from app.models import Playlist, PlaylistTrack, TrackFile


class UnsafePath(Exception):
    """A path that would leave its folder, or isn't a plain file. Nothing was done."""


class DeleteRefused(Exception):
    """Deleting was refused (ADR 0003): Releasomat didn't download the file, or it isn't a plain
    file. Nothing was done."""


# --- Rule 1: stay inside ------------------------------------------------------------------


def inside(root: Path, path: str | os.PathLike) -> Path:
    """The absolute path of ``path`` inside ``root``. Raises UnsafePath if it would leave ``root``.

    ``path`` is normally relative to ``root`` ("Artist/Artist - Title.mp3"); an absolute path is
    accepted only if it lies inside ``root``. Symlinks along the way are followed for the check,
    so a symlinked folder that points outside ``root`` is refused too. ``root`` itself is refused:
    no operation here acts on the whole folder.
    """
    text = os.fspath(path)
    if not text or "\x00" in text:
        raise UnsafePath("Empty or invalid path.")
    if ".." in Path(text).parts:
        raise UnsafePath(f"{text!r}: paths with '..' are not allowed.")
    try:
        base = Path(root).resolve(strict=True)
    except OSError:
        raise UnsafePath(f"The folder {root} doesn't exist.") from None
    relative = Path(text)
    if relative.is_absolute():
        # Spelled from the folder as configured, or from its real location.
        start = next((s for s in (Path(root), base) if relative.is_relative_to(s)), None)
        if start is None:
            raise UnsafePath(f"{text!r} is outside {base}.")
        relative = relative.relative_to(start)
    candidate = base / relative
    resolved = candidate.resolve()  # follows symlinks: a link pointing outside is caught here
    if resolved == base or not resolved.is_relative_to(base):
        raise UnsafePath(f"{text!r} is outside {base}.")
    # The path itself, not where its last part points: deleting a symlink must look at the
    # link (and refuse it), not at its target.
    return candidate


# --- File names from the path template ----------------------------------------------------

PLACEHOLDERS = ("artist", "title", "album", "ext")
# Characters that are not allowed (or cause trouble) in file names on Linux, Windows and SMB.
_BAD_CHARACTERS = str.maketrans({c: "_" for c in '/\\:*?"<>|'})
MAX_PART = 120  # characters per folder or file name; file systems allow 255 bytes


def clean_part(value: str | None) -> str:
    """One value made safe as (part of) a file or folder name: no slashes, no control
    characters, no leading or trailing dots and spaces. Empty -> "Unknown"."""
    text = "".join(c for c in (value or "") if c.isprintable()).translate(_BAD_CHARACTERS)
    text = " ".join(text.split())[:MAX_PART].strip(" .")
    return text or "Unknown"


def render_template(template: str, *, artist: str, title: str, album: str | None, ext: str) -> str:
    """The relative path for a track from PATH_TEMPLATE, e.g.
    ``"{artist}/{artist} - {title}.{ext}"`` -> ``"Daft Punk/Daft Punk - One More Time.mp3"``.

    The values are cleaned, so a title like "AC/DC" or "../x" can't add or leave folders.
    Raises ValueError for unknown placeholders or a template that gives an empty name.
    """
    names = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
    unknown = names - set(PLACEHOLDERS)
    if unknown:
        raise ValueError(f"Unknown placeholder {{{sorted(unknown)[0]}}} in the path template.")
    clean_ext = "".join(c for c in ext.lower() if c.isalnum())[:8] or "bin"
    text = template.format(
        artist=clean_part(artist),
        title=clean_part(title),
        album=clean_part(album),
        ext=clean_ext,
    )
    parts = [part.strip(" ") for part in text.split("/")]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("The path template gives an empty folder or file name.")
    return "/".join(parts)


# --- Rule 2: never overwrite --------------------------------------------------------------


def numbered(path: Path) -> Iterator[Path]:
    """``path``, then "name (2).ext", "name (3).ext", … : the names to try, in order."""
    yield path
    for n in count(2):
        yield path.with_name(f"{path.stem} ({n}){path.suffix}")


def free_name(path: Path) -> Path:
    """The first of ``numbered(path)`` that nothing (no file, folder or symlink) uses yet."""
    return next(p for p in numbered(path) if not p.exists() and not p.is_symlink())


def save_download(
    source: Path,
    source_root: Path,
    music_dir: Path,
    relative: str,
    *,
    taken: Collection[str] = (),
) -> Path:
    """Move a finished download into MUSIC_DIR and return where it ended up.

    ``source`` must be a plain file inside ``source_root`` (the download's staging folder, or
    SLSKD_DOWNLOADS_DIR for a Soulseek download Releasomat started). The target is
    ``music_dir/relative``; if that name is taken, a numbered one. ``taken`` lists more names
    (relative to MUSIC_DIR) that count as taken although no file has them now: where files
    that left the folder used to be, so Tagwerk's notices about them never mean a new file.
    Existing files are never overwritten. The source is removed only after the copy is
    complete.

    The file **appears in one step** (ADR 0009), so Navidrome and Tagwerk never see half of it:
    the final name is reserved first (an empty file), the download is copied into a hidden file
    next to it (``.<name>.releasomat-part``), its size is checked, and then it is renamed onto
    the reserved name. If anything fails, only the hidden file and the empty reservation are
    removed.
    """
    src = inside(source_root, source)
    if src.is_symlink() or not src.is_file():
        raise UnsafePath(f"{source} is not a plain file.")
    target = inside(music_dir, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    inside(music_dir, target.parent)  # still inside after creating the folders
    base = Path(music_dir).resolve()
    skip = {unicodedata.normalize("NFC", name) for name in taken}
    saved = _place_without_overwriting(src, target, lambda p: _relative_nfc(p, base) in skip)
    src.unlink()
    return saved


# Hidden files a download is copied into before it gets its real name (``save_download``).
# Nothing else ever has this ending, so leftovers (after a crash) can be removed safely.
PART_SUFFIX = ".releasomat-part"
NAME_LIMIT = 255  # bytes per file name on the usual file systems


def is_part_name(name: str) -> bool:
    """True for the names of save_download's hidden files: ``.<name>.releasomat-part``."""
    return name.startswith(".") and name.endswith(PART_SUFFIX) and len(name) > len(PART_SUFFIX) + 1


def _part_names(target: Path) -> Iterator[Path]:
    """Hidden names next to ``target`` to copy into: ``.<name>.releasomat-part``, then
    ``.<name>.2.releasomat-part``, … (shortened to stay within the file name limit)."""
    for n in count(1):
        extra = PART_SUFFIX if n == 1 else f".{n}{PART_SUFFIX}"
        name = target.name
        while len(f".{name}{extra}".encode()) > NAME_LIMIT:
            name = name[:-1]
        yield target.with_name(f".{name}{extra}")


def _relative_nfc(path: Path, base: Path) -> str:
    return unicodedata.normalize("NFC", path.relative_to(base).as_posix())


def _place_without_overwriting(
    src: Path, target: Path, also_taken: Callable[[Path], bool] = lambda p: False
) -> Path:
    """Copy ``src`` to ``target`` (or a numbered name) through a hidden file; see
    ``save_download``. Returns the final path."""
    for candidate in numbered(target):
        if also_taken(candidate):
            continue
        try:
            # "x": reserve the name, fail if anything already has it (also a symlink).
            reservation = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        reserved = os.fstat(reservation)
        os.close(reservation)
        part: Path | None = None
        try:
            for part_name in _part_names(candidate):
                try:
                    out = open(part_name, "xb")  # noqa: SIM115 (closed below)
                except FileExistsError:
                    continue
                part = part_name
                break
            assert part is not None
            with out, open(src, "rb") as inp:
                shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
            expected, written = src.stat().st_size, part.stat().st_size
            if written != expected:
                raise OSError(f"Copied {written} of {expected} bytes of {src.name}.")
            now = os.lstat(candidate)
            if (now.st_dev, now.st_ino, now.st_size) != (reserved.st_dev, reserved.st_ino, 0):
                raise FileExistsError(f"Something else took {candidate.name} meanwhile.")
            os.replace(part, candidate)  # the complete file appears under its name at once
        except BaseException:
            if part is not None:
                part.unlink(missing_ok=True)  # only our own hidden file
            _remove_reservation(candidate, reserved)
            raise
        return candidate
    raise AssertionError("unreachable")


def _remove_reservation(path: Path, reserved: os.stat_result) -> None:
    """Remove the empty file ``_place_without_overwriting`` reserved, but only if it is still
    that same empty file."""
    try:
        now = os.lstat(path)
    except FileNotFoundError:
        return
    if (now.st_dev, now.st_ino, now.st_size) == (reserved.st_dev, reserved.st_ino, 0):
        path.unlink(missing_ok=True)


def clear_part_files(music_dir: Path) -> int:
    """Remove hidden ``.<name>.releasomat-part`` files that a crash left inside MUSIC_DIR (at
    startup). Only plain files with exactly that kind of name: they are half-copied downloads,
    never music. Symlinked folders are not followed. Returns how many were removed."""
    try:
        base = Path(music_dir).resolve(strict=True)
    except OSError:
        return 0
    if not base.is_dir():
        return 0
    removed = 0
    for folder, _dirs, names in os.walk(base):  # doesn't follow symlinked folders
        for name in names:
            if not is_part_name(name):
                continue
            path = Path(folder) / name
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base):
                continue
            path.unlink()
            removed += 1
    return removed


def _copy_without_overwriting(src: Path, target: Path) -> Path:
    for candidate in numbered(target):
        try:
            # "x": create the file, fail if anything already has this name (also a symlink).
            out = open(candidate, "xb")  # noqa: SIM115 (closed below; removed again on error)
        except FileExistsError:
            continue
        try:
            with out, open(src, "rb") as inp:
                shutil.copyfileobj(inp, out)
        except BaseException:
            candidate.unlink(missing_ok=True)  # only our own half-written file
            raise
        return candidate
    raise AssertionError("unreachable")


# --- Staging: where downloads land before they are checked and saved -------------------------

STAGING = "staging"  # folder name inside CONFIG_DIR


def new_staging_dir(config_dir: Path) -> Path:
    """A new, empty folder for one download: CONFIG_DIR/staging/<random name>.
    Downloaders write only there; ``save_download`` moves the finished file into MUSIC_DIR."""
    root = Path(config_dir) / STAGING
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="dl-", dir=root.resolve()))


def discard_staged(staging_dir: Path, path: Path) -> None:
    """Remove one file from a download's staging folder (a candidate that turned out to be the
    wrong song). Only plain files inside ``staging_dir``; never anything in MUSIC_DIR."""
    target = inside(staging_dir, path)
    if target.is_file() and not target.is_symlink():
        target.unlink()


# --- Soulseek: taking finished downloads out of slskd's folder --------------------------------

# Releasomat's own folder inside SLSKD_DOWNLOADS_DIR. Every Soulseek download Releasomat starts
# asks slskd to save it in SLSKD_FOLDER/<download id>/ (a "batch destination"), so a file in there
# is always one Releasomat asked for. Everything else in slskd's folder belongs to the owner.
SLSKD_FOLDER = "releasomat"


def take_slskd_download(downloads_dir: Path, path: str | os.PathLike, staging_dir: Path) -> Path:
    """Move a finished Soulseek download out of slskd's folder into the download's staging folder
    and return its new path.

    Only plain files inside ``downloads_dir/releasomat/`` are accepted: the folder slskd puts
    Releasomat's own downloads in (ADR 0003: from SLSKD_DOWNLOADS_DIR, Releasomat only moves files
    of downloads it started). Anything else raises UnsafePath and nothing is done. The file is
    copied without overwriting anything, then removed from slskd's folder, and its emptied
    download folder (``releasomat/<id>``) goes too; ``releasomat`` itself stays.
    """
    own = inside(downloads_dir, SLSKD_FOLDER)
    if own.is_symlink() or not own.is_dir():
        raise UnsafePath(f"{own} is not a folder.")
    src = inside(own, path)
    if src.is_symlink() or not src.is_file():
        raise UnsafePath(f"{path} is not a plain file.")
    if not os.access(src.parent, os.W_OK | os.X_OK):
        # Checked first, so a copy isn't made that can't be finished.
        raise PermissionError(
            f"Releasomat may not remove files from {src.parent} (slskd and Releasomat must run "
            "as the same user: PUID/PGID)."
        )
    target = inside(staging_dir, src.name)
    saved = _copy_without_overwriting(src, target)
    src.unlink()
    _remove_empty_folders(src.parent, own.resolve())
    return saved


def clear_staging(config_dir: Path, staging_dir: Path | None = None) -> None:
    """Remove a download's staging folder with whatever is left in it (half-finished downloads,
    thumbnails). Without ``staging_dir``: every staging folder (at startup, after a crash).
    Only ever inside CONFIG_DIR/staging, which holds nothing but Releasomat's own downloads."""
    root = Path(config_dir) / STAGING
    if not root.is_dir() or root.is_symlink():
        return
    folders = [inside(root, staging_dir)] if staging_dir else list(root.iterdir())
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            continue
        if not folder.resolve().is_relative_to(root.resolve()):
            continue
        shutil.rmtree(folder, ignore_errors=True)


# --- Rule 3: never delete on its own; only the owner deletes ------------------------------


@dataclass(frozen=True)
class OwnerDelete:
    """What ``delete_by_owner`` did: the deleted file, and the playlists that still list the
    track (the confirmation names them; their Navidrome playlists lose the song)."""

    path: Path
    still_in: list[str] = field(default_factory=list)


def playlists_with(session: Session, track_id: int) -> list[str]:
    """Titles of the playlists that still contain a track (it hasn't left them)."""
    rows = session.exec(
        select(Playlist.title)
        .join(PlaylistTrack, PlaylistTrack.playlist_id == Playlist.id)
        .where(PlaylistTrack.track_id == track_id, PlaylistTrack.removed_at.is_(None))
    ).all()
    return sorted(set(rows))


def delete_by_owner(session: Session, track_file: TrackFile, music_dir: Path) -> OwnerDelete:
    """Delete a downloaded file because **the owner** asked for it (a delete button, after a
    confirmation; phase 4). Nothing in a refresh, schedule or playlist change calls this.

    Refuses (DeleteRefused / UnsafePath, nothing done) unless Releasomat downloaded the file
    itself and it is a plain file inside MUSIC_DIR. Songs that were already in the library have
    no path and are never touched. Folders left empty are removed too (never MUSIC_DIR itself).
    The caller updates the ``track_file`` row afterwards.
    """
    if not track_file.downloaded_by_releasomat or not track_file.path:
        raise DeleteRefused("Releasomat didn't download this file, so it never deletes it.")
    still_in = playlists_with(session, track_file.track_id)
    target = inside(music_dir, track_file.path)
    if target.is_symlink() or not target.is_file():
        raise DeleteRefused(f"{track_file.path} is not a plain file.")
    target.unlink()
    _remove_empty_folders(target.parent, Path(music_dir).resolve())
    return OwnerDelete(target, still_in)


def _remove_empty_folders(folder: Path, root: Path) -> None:
    while folder != root and folder.is_relative_to(root) and not folder.is_symlink():
        try:
            folder.rmdir()  # only works when the folder is empty
        except OSError:
            return
        folder = folder.parent
