"""One refresh of one playlist (docs/architecture.md, "What one refresh does").

1. **read** the playlist from its source; store its tracks and what changed since last time
2. **match**: for every track without a song in Navidrome yet, search Navidrome; a song that is
   already there is linked and never touched. Downloads whose file has left Releasomat's folder
   are sorted out first (``files_that_left``); adopted and "Don't like" tracks are never
   downloaded, only linked when the song is elsewhere in the library (ADR 0009,
   ``app/lifecycle.py``)
3. **download** the missing ones with the download sources in the global order (Settings →
   Download sources, ADR 0008), tag them and save them under MUSIC_DIR (``app/files.py``)
4. **scan**: let Navidrome scan Releasomat's folder, then find the new songs
5. **playlist**: create or update the Navidrome playlist in the source's order
6. tracks that left the playlist only leave the Navidrome playlist: **files are never deleted**
   (ADR 0003)

Every refresh is recorded as a ``run`` (Activity page), and its progress is published as events,
so open pages update live. A refresh never raises: whatever goes wrong ends the run as "failed"
with a plain message and an error report (``app/reports.py``). If reading the playlist fails,
nothing in Navidrome changes: the playlist keeps its last good state.

The owner can **stop** a refresh (``StopButton``): it ends at once as "stopped". Downloads that
were already saved stay (the next refresh links them), a half-finished one is thrown away, and the
Navidrome playlist is left as it is.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, select

from app import files, httpclient, lifecycle, matching, reports, tagging
from app import settings as user_settings
from app.config import Config, get_config
from app.db import get_engine
from app.downloaders.base import Downloader, DownloaderError, DownloaderNotReady
from app.downloaders.slskd import SlskdDownloader
from app.downloaders.youtube import YouTubeDownloader
from app.events import EventBus, bus
from app.models import (
    DislikeReason,
    FileStatus,
    NavidromeMode,
    NavidromeSnapshot,
    Playlist,
    PlaylistTrack,
    Run,
    RunStatus,
    Track,
    TrackFile,
    utcnow,
)
from app.navidrome import Navidrome, NavidromeError
from app.playlists import track_counts
from app.sources import get_source, label
from app.sources.base import PlaylistRef, PlaylistSnapshot, SourceTrack

log = logging.getLogger(__name__)

COVER_LIMIT = 5_000_000  # bytes; bigger pictures are not embedded
# Tracks being downloaded right now by any refresh in this process: track ID -> an event that is
# set when that download has ended. A refresh that needs the same track waits for it instead of
# downloading it twice. Kept in memory, not as a status in the database: a stopped or crashed
# refresh can't leave a "downloading" mark behind that other refreshes would wait for forever.
DOWNLOADING_NOW: dict[int, asyncio.Event] = {}
USABLE = (FileStatus.DONE.value, FileStatus.IN_LIBRARY.value)
KEPT_AWAY = lifecycle.KEPT_AWAY  # adopted and "Don't like": never downloaded
# The safety brake of ``files_that_left``: when every downloaded file of a playlist is gone at
# once, and at least this many, the folder is more likely unmounted than emptied by the owner.
BRAKE_MINIMUM = 5
UNMOUNTED = "Releasomat's folder looks unmounted; nothing was marked Don't like."
# Why a track is missing again when its download was cut off (shown on the playlist page).
CUT_OFF = "The refresh was stopped before this download finished. The next refresh tries again."


async def fetch_cover(url: str | None) -> bytes | None:
    """The cover picture for tagging (JPEG or PNG), or None if there is none or it fails."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        async with httpclient.client() as http:
            response = await http.get(url)
    except Exception as exc:  # no cover is no reason to fail a download
        log.info("Cover %s not loaded: %s", url, type(exc).__name__)
        return None
    if response.status_code != 200 or len(response.content) > COVER_LIMIT:
        return None
    return response.content if tagging.cover_mime(response.content) else None


@dataclass
class Services:
    """What a refresh works with. Normally made from the settings (``from_settings``); tests
    pass fakes for Navidrome, the downloaders and the cover download."""

    navidrome: Navidrome
    # The download sources that are switched on, in the order they are tried (DOWNLOAD_SOURCES).
    downloaders: list[Downloader]
    config: Config
    slots: asyncio.Semaphore  # downloads at the same time, shared by all refreshes
    threshold: int = 85
    path_template: str = "{artist}/{artist} - {title}.{ext}"
    snapshot_auto_delete: bool = False
    secrets: list[str] = field(default_factory=list)
    cover: Callable[[str | None], Awaitable[bytes | None]] = fetch_cover

    @classmethod
    def from_settings(
        cls, session: Session, config: Config, slots: asyncio.Semaphore | None = None
    ) -> "Services":
        threshold = user_settings.get_int(session, "MATCH_THRESHOLD")
        concurrency = user_settings.get_int(session, "DOWNLOAD_CONCURRENCY")
        navidrome = Navidrome.from_settings(session)
        return cls(
            navidrome=navidrome,
            downloaders=download_sources(session, config, threshold),
            config=config,
            slots=slots or asyncio.Semaphore(concurrency),
            threshold=threshold,
            path_template=user_settings.get(session, "PATH_TEMPLATE"),
            snapshot_auto_delete=user_settings.get_bool(session, "SNAPSHOT_AUTO_DELETE"),
            secrets=reports.known_secrets(session, config.config_dir),
        )


def download_sources(session: Session, config: Config, threshold: int) -> list[Downloader]:
    """The download sources switched on in Settings (DOWNLOAD_SOURCES), in their order. Soulseek
    is made even without an address or API key: the refresh then skips it with a note."""
    sources: list[Downloader] = []
    for name in user_settings.get_list(session, "DOWNLOAD_SOURCES"):
        if name == "slskd":
            sources.append(
                SlskdDownloader(
                    url=user_settings.get(session, "SLSKD_URL") or None,
                    api_key=user_settings.get(session, "SLSKD_API_KEY") or None,
                    downloads_dir=config.slskd_downloads_dir,
                    formats=user_settings.get_list(session, "SLSKD_FORMATS"),
                    min_bitrate=user_settings.get_int(session, "SLSKD_MIN_BITRATE"),
                    threshold=threshold,
                )
            )
        elif name == "youtube":
            sources.append(YouTubeDownloader(config_dir=config.config_dir, threshold=threshold))
    return sources


def is_connected(downloader: Downloader) -> bool:
    """Whether a download source can be used (Soulseek needs slskd's address and API key)."""
    return getattr(downloader, "configured", True)


class StepFailed(Exception):
    """A step failed; the run ends as failed with this report."""

    def __init__(self, report: dict):
        super().__init__(report["message"])
        self.report = report


class StopButton:
    """The Stop button of one refresh. The worker (``app/worker.py``) makes one for every refresh
    and presses it when the owner asks.

    Pressing it cancels the refresh's asyncio task. Whatever the refresh is waiting for right now
    is cancelled with it: a YouTube download's worker process is ended, a Soulseek search or
    transfer is cancelled in slskd, the wait for Navidrome's scan ends. On the way out, each step
    cleans up after itself (``except asyncio.CancelledError`` and ``finally`` blocks), and
    ``refresh_playlist`` ends the run as "stopped".

    Once the last step has started to save the Navidrome playlist, it is too late: that step is
    short and finishes, so Navidrome never gets half a playlist.
    """

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None  # the running refresh (set by the worker)
        self.run_id: int | None = None  # its run, once it has one
        self.pressed = False
        self.too_late = False  # set by the refresh when it starts saving the Navidrome playlist

    def press(self) -> bool:
        """Stop the refresh now. False when it is too late to stop it."""
        if self.too_late:
            return False
        if not self.pressed:
            self.pressed = True
            if self.task is not None:
                self.task.cancel()
        return True


async def refresh_playlist(
    playlist_id: int,
    trigger: str,
    *,
    engine: Engine | None = None,
    events: EventBus = bus,
    services: Services | None = None,
    slots: asyncio.Semaphore | None = None,
    stop: StopButton | None = None,
) -> int | None:
    """Refresh one playlist now and return the run's ID (None if the playlist doesn't exist).
    The worker makes sure the same playlist never refreshes twice at the same time.

    When ``stop`` is pressed, the run ends as "stopped" and the cancellation goes on to the
    caller (``asyncio.CancelledError``). A cancellation without it (Releasomat shutting down)
    leaves the run as it is: ``mark_interrupted`` ends it at the next start."""
    config = get_config()
    engine = engine or get_engine(config.database_url)
    with Session(engine) as session:
        playlist = session.get(Playlist, playlist_id)
        if playlist is None:
            log.warning("Refresh of playlist %s skipped: it doesn't exist (any more)", playlist_id)
            return None
        run = Run(playlist_id=playlist_id, trigger=trigger, status=RunStatus.RUNNING.value)
        run.message = "Reading the playlist…"
        run.add_log(f"{_clock()} Refresh started ({trigger}).")
        playlist.last_status = RunStatus.RUNNING.value
        session.add(run)
        session.commit()
        run_id = run.id
        services = services or Services.from_settings(session, config, slots)
    events.publish("run", run_id=run_id, playlist_id=playlist_id, status=RunStatus.RUNNING.value)
    refresh = Refresh(engine, events, services, playlist_id, run_id, stop)
    if stop is not None:
        stop.run_id = run_id
    try:
        await refresh.run()
    except asyncio.CancelledError:
        if stop is not None and stop.pressed:
            refresh.stopped()
        raise
    except StepFailed as exc:
        refresh.finish(RunStatus.FAILED, str(exc), exc.report)
    except Exception as exc:
        log.exception("Refresh of playlist %s failed", playlist_id)
        report = reports.failure(refresh.step, exc, secrets=services.secrets)
        refresh.finish(RunStatus.FAILED, report["message"], report)
    return run_id


@dataclass
class TrackState:
    """One track of this refresh, in the playlist's order."""

    position: int
    track: Track
    song_id: str | None = None
    status: str = FileStatus.PENDING.value
    path: str | None = None  # where a download was saved, relative to MUSIC_DIR
    message: str | None = None
    tried: list[dict] = field(default_factory=list)
    new_download: bool = False
    # The track's old song in Releasomat's folder after its file left: Navidrome lists it until
    # its next scan, so it is only linked once Navidrome says it is elsewhere in the library.
    skip_ids: set[str] = field(default_factory=set)
    searched: bool = False  # already looked for outside Releasomat's folder in this refresh
    # An adopted track's old song, kept in the Navidrome playlist while its file waits in
    # Tagwerk's inbox: Navidrome hides it meanwhile and shows it again in its place when the
    # file comes back with the same ID (ADR 0010).
    held_id: str | None = None


class Refresh:
    """The steps of one refresh. Each step keeps its database work short (one session each),
    so pages stay responsive while downloads run."""

    def __init__(
        self,
        engine,
        events,
        services: Services,
        playlist_id: int,
        run_id: int,
        stop: StopButton | None = None,
    ):
        self.engine = engine
        self.events = events
        self.services = services
        self.playlist_id = playlist_id
        self.run_id = run_id
        self.stop = stop
        self.step = "read"
        self.states: list[TrackState] = []
        self.left = 0  # tracks that left the playlist this time
        self.warnings: list[str] = []
        self.sources: list[Downloader] = []  # the download sources this refresh can use
        self.to_download = 0  # missing tracks the download step works on
        self.handled = 0  # of those, how many are done (downloaded, failed or skipped)
        self.cut_off: list[str] = []  # downloads a stop ended halfway ("Artist – Title (YouTube)")
        # What happened to files that left Releasomat's folder, for the run's message.
        self.changes = {"adopted": 0, "moved": 0, "vanished": 0}
        with Session(engine) as session:
            playlist = session.get(Playlist, playlist_id)
            self.title = playlist.title
            self.url = playlist.url
            self.source = playlist.source
            self.ref = PlaylistRef(
                source=playlist.source, id=playlist.source_id, url=playlist.url, kind=playlist.kind
            )
        self.source_label = label(self.source)

    # --- Bookkeeping --------------------------------------------------------------------------

    def log(self, line: str, *, progress: str | None = None) -> None:
        """Add a line to the run's log; ``progress`` replaces the run's message while it runs."""
        with Session(self.engine) as session:
            run = session.get(Run, self.run_id)
            if run is None:
                return
            if line:
                run.add_log(f"{_clock()} {line}")
            if progress:
                run.message = progress
            session.commit()
        if progress:
            self.events.publish(
                "run", run_id=self.run_id, playlist_id=self.playlist_id, status="running",
                message=progress,
            )  # fmt: skip

    def track_event(self, state: TrackState) -> None:
        self.events.publish(
            f"track-{self.playlist_id}",
            run_id=self.run_id,
            track_id=state.track.id,
            position=state.position,
            status=state.status,
        )

    def failed(self, step: str, exc: BaseException, prefix: str = "Navidrome") -> StepFailed:
        return StepFailed(
            reports.failure(
                step,
                exc,
                prefix=prefix,
                source=self.source_label,
                link=self.url,
                secrets=self.services.secrets + self.services.navidrome.secrets,
            )
        )

    def finish(self, status: RunStatus, message: str, report: dict | None = None) -> None:
        with Session(self.engine) as session:
            run = session.get(Run, self.run_id)
            if run is None:  # the playlist was removed while it refreshed
                return
            run.status = status.value
            run.message = message
            run.report = report
            run.finished_at = utcnow()
            run.add_log(f"{_clock()} {message}")
            counts = track_counts(session, self.playlist_id)
            run.tracks_total = counts["total"]
            run.tracks_in_library = counts["in_library"]
            run.tracks_downloaded = counts["downloaded"]
            run.tracks_missing = counts["missing"]
            run.tracks_failed = counts["failed"]
            playlist = session.get(Playlist, self.playlist_id)
            if playlist is not None:
                playlist.last_status = status.value
                playlist.last_refresh_at = run.finished_at
            session.commit()
        level = logging.WARNING if status == RunStatus.FAILED else logging.INFO
        log.log(level, "Playlist %s: %s: %s", self.playlist_id, status.value, message)
        self.events.publish(
            "run",
            run_id=self.run_id,
            playlist_id=self.playlist_id,
            status=status.value,
            message=message,
        )

    def stopped(self) -> None:
        """The owner pressed Stop: the run ends as "stopped", with what was done until then.
        Downloads that were saved stay (their tracks are linked next time); the Navidrome
        playlist was not changed."""
        new = sum(s.new_download for s in self.states)
        failed = sum(s.status == FileStatus.FAILED for s in self.states)
        if self.step == "read":
            when = f"while reading the playlist from {self.source_label}"
        elif self.step == "match":
            when = "while looking for the songs in Navidrome"
        elif self.step == "download":
            outcome = f"{new} downloaded" + (f", {failed} failed" if failed else "")
            when = (
                f"after {self.handled} of {count(self.to_download, 'track')} to download "
                f"({outcome})"
            )
        else:
            when = f"while Navidrome was scanning ({new} downloaded)"
        message = f"Stopped by you {when}. The Navidrome playlist was left as it is."
        if new:
            message += " The downloaded songs stay and are added with the next refresh."
        details = {
            "stopped_during": reports.STEPS.get(self.step, self.step),
            "cut_off_downloads": "; ".join(self.cut_off),
        }
        report = reports.stopped(
            message,
            self.step,
            details=details,
            failed=self.failed_tracks(),
            source=self.source_label,
            link=self.url,
            secrets=self.services.secrets + self.services.navidrome.secrets,
        )
        self.finish(RunStatus.STOPPED, message, report)

    def failed_tracks(self) -> list[dict]:
        """The tracks that failed to download, for the report."""
        return [
            {
                "position": s.position,
                "artist": s.track.artist,
                "title": s.track.title,
                "message": s.message,
                "tried": s.tried,
            }
            for s in self.states
            if s.status == FileStatus.FAILED
        ]

    # --- The steps ----------------------------------------------------------------------------

    async def run(self) -> None:
        snapshot = await self.read()
        self.store(snapshot)
        if not self.services.navidrome.configured:
            raise self.failed(
                "match",
                NavidromeError("Navidrome isn't set up: enter its address, user and password."),
            )
        await self.match()
        await self.download()
        await self.scan_and_find()
        message = await self.update_playlist(snapshot)
        failed = self.failed_tracks()
        report = None
        if failed:
            report = reports.for_tracks(
                failed,
                source=self.source_label,
                link=self.url,
                secrets=self.services.secrets + self.services.navidrome.secrets,
            )
        self.finish(RunStatus.DONE, message, report)

    async def read(self) -> PlaylistSnapshot:
        """Step 1: the playlist as the service lists it now."""
        self.step = "read"
        try:
            snapshot = await get_source(self.ref.source).fetch(self.ref)
        except Exception as exc:
            if not hasattr(exc, "details"):
                log.exception("Reading playlist %s failed", self.playlist_id)
            raise self.failed("read", exc, prefix=self.source_label) from None
        return snapshot

    def store(self, snapshot: PlaylistSnapshot) -> None:
        """Step 1b: save the tracks and what changed since the last refresh. Tracks that left
        only get ``removed_at``: their files stay (ADR 0003)."""
        now = utcnow()
        with Session(self.engine) as session:
            playlist = session.get(Playlist, self.playlist_id)
            playlist.title = snapshot.title or playlist.title
            playlist.cover_url = snapshot.cover_url or playlist.cover_url
            self.title = playlist.title
            order: list[Track] = []
            for source_track in snapshot.tracks:
                track = upsert_track(session, source_track)
                if all(t.id != track.id for t in order):  # a track listed twice counts once
                    order.append(track)
            rows = session.exec(
                select(PlaylistTrack).where(PlaylistTrack.playlist_id == self.playlist_id)
            ).all()
            entries: dict[int, PlaylistTrack] = {}
            for row in sorted(rows, key=lambda r: (r.removed_at is None, r.id or 0)):
                entries[row.track_id] = row  # the current entry wins over old ones
            added = 0
            for position, track in enumerate(order, start=1):
                entry = entries.get(track.id)
                if entry is None:
                    session.add(
                        PlaylistTrack(
                            playlist_id=self.playlist_id,
                            track_id=track.id,
                            position=position,
                            added_at=now,
                        )
                    )
                    added += 1
                else:
                    if entry.removed_at is not None:
                        entry.removed_at, entry.added_at = None, now
                        added += 1
                    entry.position = position
            wanted = {t.id for t in order}
            self.left = 0
            for track_id, entry in entries.items():
                if track_id not in wanted and entry.removed_at is None:
                    entry.removed_at = now
                    self.left += 1
            session.commit()
            for track in order:
                session.refresh(track)
            self.states = [TrackState(i, t) for i, t in enumerate(order, start=1)]
        changes = f"{added} new" if added else "no new tracks"
        if self.left:
            changes += f", {self.left} left the playlist (their files stay)"
        self.log(
            f"Read {count(len(order), 'track')} from {self.source_label} ({changes}).",
            progress="Looking for the songs in Navidrome…",
        )

    async def match(self) -> None:
        """Step 2: link tracks Releasomat already has; search Navidrome for the others.

        Downloads whose file has left Releasomat's folder go to ``files_that_left`` first.
        Adopted and "Don't like" tracks are only looked for outside Releasomat's folder
        (``relink``) and never downloaded."""
        self.step = "match"
        known = linked = 0
        music_dir = self.services.config.music_dir
        gone: list[TrackState] = []
        expected = 0  # downloads of this playlist that should be in Releasomat's folder
        with Session(self.engine) as session:
            for state in self.states:
                file = file_of(session, state.track.id)
                if file is None:
                    continue
                old_id = (file.details or {}).get("old_song_id")
                state.skip_ids = {old_id} if old_id else set()
                if file.status == FileStatus.DONE and file.downloaded_by_releasomat and file.path:
                    expected += 1
                    if not _exists(music_dir, file):
                        state.status, state.path = file.status, file.path
                        state.song_id = file.navidrome_song_id
                        gone.append(state)
                        continue
                if file.status in KEPT_AWAY:
                    state.status = file.status
                elif file.status in USABLE and file.navidrome_song_id:
                    state.song_id, state.status = file.navidrome_song_id, file.status
                    state.path = file.path
                    known += 1
                elif file.status == FileStatus.DONE and file.path:
                    state.status, state.path = FileStatus.DONE.value, file.path  # find after scan
        if gone:
            await self.files_that_left(gone, expected)
        await self.relink([s for s in self.states if s.status in KEPT_AWAY and not s.searched])
        for state in self.states:
            if state.song_id or state.path or state.status in KEPT_AWAY:
                continue
            try:
                found = await self.services.navidrome.find_in_library(
                    state.track, self.services.threshold, skip_ids=state.skip_ids
                )
            except NavidromeError as exc:
                raise self.failed("match", exc) from None
            if found is None:
                continue
            state.song_id, state.status = found.song["id"], FileStatus.IN_LIBRARY.value
            linked += 1
            self.save_file(
                state,
                status=state.status,
                navidrome_song_id=found.song["id"],
                downloader=None,
                format=found.song.get("suffix"),
                bitrate=found.song.get("bitRate"),
                details={"matched_by": found.method, "score": found.score, "tried": []},
                error=None,
            )
            self.track_event(state)
        missing = len(self.missing())
        next_step = f"Downloading {count(missing, 'track')}…" if missing else "Updating Navidrome…"
        self.log(
            f"Linked before: {known}. Found in Navidrome now: {linked}. Missing: {missing}.",
            progress=next_step,
        )

    def missing(self) -> list[TrackState]:
        """Tracks without a song in Navidrome or a file, that may be downloaded."""
        return [
            s for s in self.states if not s.song_id and not s.path and s.status not in KEPT_AWAY
        ]

    async def files_that_left(self, gone: list[TrackState], expected: int) -> None:
        """Downloads whose file is no longer in Releasomat's folder, without an adoption notice
        (ADR 0009). The owner moved it into the library: its copy there is linked. Not found
        anywhere: "Don't like" (it was deleted), lifted by ``relink`` if the song turns up later.
        Releasomat never downloads such a track again on its own.

        **Safety brake**: when the folder looks unmounted (missing, without a single file, or
        every expected file of this playlist gone at once, at least ``BRAKE_MINIMUM``), nothing
        is decided and the run warns; the tracks stay as they were."""
        music_dir = Path(self.services.config.music_dir)
        all_gone = len(gone) == expected and len(gone) >= BRAKE_MINIMUM
        if all_gone or not _has_files(music_dir):
            self.warnings.append(UNMOUNTED)
            self.log(f"{UNMOUNTED} ({count(len(gone), 'downloaded file')} not found.)")
            return
        navidrome, threshold = self.services.navidrome, self.services.threshold
        moved = vanished = 0
        for state in gone:
            path, skip = state.path, state.skip_ids | ({state.song_id} - {None})
            try:
                found = await navidrome.find_elsewhere(state.track, threshold, skip_ids=skip)
            except NavidromeError as exc:
                raise self.failed("match", exc) from None
            with Session(self.engine) as session:
                file = file_of(session, state.track.id)
                if found is not None:
                    result = lifecycle.left_folder(
                        session, file, path, found.song, matched_by=found.method, score=found.score
                    )
                else:
                    result = lifecycle.left_folder(session, file, path)
                self.take_row(state, file)
            state.searched = True
            if result.changed and found is not None:
                moved += 1
                self.log(f"{_name(state)}: moved into your library (linked there).")
            elif result.changed:
                vanished += 1
                self.log(f"{_name(state)}: its file was deleted from Releasomat's folder.")
            else:  # an adoption notice came first: it is looked for like any adopted track
                state.searched = False
            self.track_event(state)
        self.changes["moved"] += moved
        self.changes["vanished"] += vanished

    async def relink(self, states: list[TrackState]) -> None:
        """Adopted and "Don't like" tracks: their song is looked for outside Releasomat's folder
        and linked when found (the Navidrome playlist then uses the library copy).

        An adopted track first asks Navidrome about its old song: when Tagwerk's import kept
        title and album, Navidrome finds the file again and keeps the song's ID, plays and
        stars (ADR 0010), and that song is linked as it is. Until then the old song keeps its
        place in the Navidrome playlist (``held_id``)."""
        if not states:
            return
        navidrome, threshold = self.services.navidrome, self.services.threshold
        adopted = moved = others = 0
        for state in states:
            old_id = self.old_song(state)
            song = await self.song_back(old_id) if old_id else None
            method, score = "same_song", None
            if song is None:
                try:
                    found = await navidrome.find_elsewhere(
                        state.track, threshold, skip_ids=state.skip_ids
                    )
                except NavidromeError as exc:
                    raise self.failed("match", exc) from None
                if found is not None:
                    song, method, score = found.song, found.method, found.score
            state.searched = True
            if song is None:
                state.held_id = old_id
                continue
            with Session(self.engine) as session:
                file = file_of(session, state.track.id)
                guessed = file.disliked_reason == DislikeReason.VANISHED
                was_adopted = file.status == FileStatus.ADOPTED
                result = lifecycle.link_library_copy(
                    session, file, song, matched_by=method, score=score
                )
                self.take_row(state, file)
            if not result.changed:
                continue
            if guessed:
                moved += 1
            elif was_adopted:
                adopted += 1
            else:
                others += 1
            if method == "same_song":
                self.log(f"{_name(state)}: in your library now, the same song in Navidrome.")
            else:
                self.log(f"{_name(state)}: found in your library, linked.")
            self.track_event(state)
        self.changes["adopted"] += adopted
        self.changes["moved"] += moved
        if others:
            self.log(f"Don't like, but in your library: {count(others, 'track')} linked.")

    def old_song(self, state: TrackState) -> str | None:
        """An adopted track's song from before the adoption: Navidrome's ID of its file in
        Releasomat's folder (``details.old_song_id``). None for every other track."""
        if state.status != FileStatus.ADOPTED:
            return None
        with Session(self.engine) as session:
            file = file_of(session, state.track.id)
            return (file.details or {}).get("old_song_id") if file else None

    async def song_back(self, song_id: str) -> dict | None:
        """The old song of an adopted track if Navidrome has it in the library now (it kept
        the ID when the file came back), else None. Only a shortcut: if Navidrome's own API
        doesn't answer, the search by name still finds the library copy."""
        try:
            return await self.services.navidrome.song_elsewhere(song_id)
        except NavidromeError as exc:
            log.info("Can't check where song %s is (%s)", song_id, exc)
            return None

    def take_row(self, state: TrackState, file: TrackFile) -> None:
        """The refresh's view of a track after its row changed (``app/lifecycle.py``)."""
        state.status = file.status
        usable = file.status in USABLE
        state.song_id = file.navidrome_song_id if usable else None
        state.path = file.path if file.status == FileStatus.DONE else None

    def notes(self) -> str:
        """What happened to files that left Releasomat's folder, for the run's message."""
        notes = []
        if self.changes["adopted"]:
            notes.append(f"{self.changes['adopted']} adopted into your library.")
        if self.changes["moved"]:
            notes.append(f"{self.changes['moved']} moved into your library.")
        if vanished := self.changes["vanished"]:
            files_were = "its file was" if vanished == 1 else "their files were"
            notes.append(
                f"{vanished} marked Don't like ({files_were} deleted from Releasomat's folder)."
            )
        return " ".join(notes)

    async def download(self) -> None:
        """Step 3: download the missing tracks, several at once (DOWNLOAD_CONCURRENCY)."""
        self.step = "download"
        todo = self.missing()
        if not todo:
            return
        if not self.services.downloaders:
            for state in todo:
                state.message = "No download source is switched on (Settings)."
            self.log(
                f"No download source is switched on: {count(len(todo), 'missing track')} "
                f"{'stays' if len(todo) == 1 else 'stay'} missing."
            )
            return
        self.sources = [d for d in self.services.downloaders if is_connected(d)]
        if not self.sources:
            self.skip_all(todo)
            return
        skipped = ", ".join(d.label for d in self.services.downloaders if not is_connected(d))
        if skipped:  # one note per run, not one per track
            self.log(f"Skipped, not connected: {skipped} (address or API key missing in Settings).")
            self.warnings.append(f"Skipped, not connected: {skipped}.")
        self.to_download, self.handled = len(todo), 0

        async def one(state: TrackState) -> None:
            await self.download_track(state)
            self.handled += 1
            self.log(
                f"{state.track.artist} – {state.track.title}: {state.message or state.status}",
                progress=f"Downloading: {self.handled} of {len(todo)} done…",
            )

        # When the refresh is stopped, gather cancels every download: the running ones end, the
        # ones still waiting for a free slot never start.
        outcomes = await asyncio.gather(*(one(s) for s in todo), return_exceptions=True)
        for state, outcome in zip(todo, outcomes, strict=True):
            if isinstance(outcome, Exception):
                log.error("Downloading %s failed", state.track.title, exc_info=outcome)
                state.status = FileStatus.FAILED.value
                state.message = f"Unexpected error: {type(outcome).__name__}: {outcome}"

    def skip_all(self, todo: list[TrackState]) -> None:
        """No switched-on download source is connected: the missing tracks stay missing (not
        failed); the run says why once."""
        names = ", ".join(d.label for d in self.services.downloaders)
        verb = "isn't" if len(self.services.downloaders) == 1 else "aren't"
        message = f"No download source is available: {names} {verb} connected."
        for state in todo:
            state.status, state.message = FileStatus.SKIPPED.value, message
            self.save_file(
                state, status=state.status, error=message, details={"tried": [], "reasons": []}
            )
            self.track_event(state)
        self.log(
            f"{message} {count(len(todo), 'track')} {'stays' if len(todo) == 1 else 'stay'} "
            "missing."
        )
        self.warnings.append(message)

    async def download_track(self, state: TrackState) -> None:
        """Try the download sources in the global order until one gives a checked file; tag it
        and save it under MUSIC_DIR. The track keeps its download slot for the whole order, so
        when Soulseek has nothing, YouTube follows right away instead of after every other
        track's Soulseek search."""
        track = state.track
        if await self.downloaded_meanwhile(state):
            return
        ended = DOWNLOADING_NOW[track.id] = asyncio.Event()
        try:
            async with self.services.slots:
                await self.try_sources(state)
        finally:
            DOWNLOADING_NOW.pop(track.id, None)
            ended.set()
            if state.status == FileStatus.DOWNLOADING:  # cut off by Stop: missing again
                state.status = FileStatus.PENDING.value
                self.save_file(state, status=state.status, error=CUT_OFF)

    async def try_sources(self, state: TrackState) -> None:
        track = state.track
        wanted = SourceTrack(
            artist=track.artist,
            title=track.title,
            album=track.album,
            duration=track.duration,
            isrc=track.isrc,
            mbid=track.mbid,
            cover_url=track.cover_url,
        )
        problems: list[str] = []
        not_ready: list[str] = []
        for downloader in self.sources:
            name = downloader.name
            state.status = FileStatus.DOWNLOADING.value
            # A new try: why an earlier one failed doesn't apply any more.
            self.save_file(
                state,
                status=state.status,
                downloader=name,
                error=None,
                details={"tried": [], "reasons": []},
            )
            self.track_event(state)
            staging = files.new_staging_dir(self.services.config.config_dir)
            try:
                result = await downloader.download(wanted, staging)
                # Saving the file is the last thing keep() does, and nothing waits between
                # that and recording it as done below: a stop can't come in between.
                saved = await self.keep(track, result, staging)
            except asyncio.CancelledError:
                # Stopped: the downloader has ended its part (process, transfer); the
                # half-finished file goes with the staging folder (finally). The track counts
                # as missing again, as if this download had never started.
                self.cut_off.append(f"{track.artist} – {track.title} ({downloader.label})")
                state.status = FileStatus.PENDING.value
                self.save_file(state, status=state.status, error=CUT_OFF)
                raise
            except DownloaderNotReady as exc:
                not_ready.append(f"{downloader.label}: {exc}")
                state.tried.append({"downloader": name, "candidate": "", "reason": str(exc)})
                continue
            except DownloaderError as exc:
                problems.append(f"{downloader.label}: {exc}")
                state.tried += exc.tried
                continue
            except (tagging.TaggingError, files.UnsafePath, ValueError, OSError) as exc:
                problems.append(f"{downloader.label}: saving the file failed: {exc}")
                continue
            except Exception as exc:
                log.exception("Downloading %s with %s failed", track.title, name)
                problems.append(f"{downloader.label}: {type(exc).__name__}: {exc}")
                continue
            finally:
                files.clear_staging(self.services.config.config_dir, staging)
            state.status, state.path, state.new_download = FileStatus.DONE.value, saved, True
            state.message = None
            self.save_file(
                state,
                status=state.status,
                path=saved,
                downloaded_by_releasomat=True,
                downloader=name,
                format=result.format,
                bitrate=result.bitrate,
                error=None,
                # Where it came from stays here, never in the file (ADR 0009).
                details={"origin": result.origin, "downloaded_at": utcnow().isoformat()},
                attempt=True,
            )
            self.track_event(state)
            return
        reasons = problems + not_ready  # one per download source, in the order tried
        if problems:
            state.status = FileStatus.FAILED.value
            state.message = " / ".join(reasons)
        else:
            state.status = FileStatus.SKIPPED.value
            state.message = " / ".join(reasons) or "No downloader could be used."
        self.save_file(
            state,
            status=state.status,
            error=state.message,
            details={"tried": state.tried, "reasons": reasons},
            attempt=bool(problems),
        )
        self.track_event(state)

    async def downloaded_meanwhile(self, state: TrackState) -> bool:
        """When another refresh is downloading the same track right now, wait for it and use its
        file instead of downloading the track twice. Between the last look and the caller
        claiming the track nothing waits, so two refreshes can't both start it."""
        while (other := DOWNLOADING_NOW.get(state.track.id)) is not None:
            await other.wait()
        with Session(self.engine) as session:
            file = file_of(session, state.track.id)
            status = file.status if file else None
            path, song_id = (file.path, file.navidrome_song_id) if file else (None, None)
        if status == FileStatus.DONE and path:
            state.status, state.path, state.song_id = FileStatus.DONE.value, path, song_id
            self.track_event(state)
            return True
        if status in KEPT_AWAY:  # adopted or "Don't like" meanwhile: never downloaded
            state.status = status
            return True
        return False

    async def keep(self, track: Track, result, staging: Path) -> str:
        """Tag a checked download and move it into MUSIC_DIR; returns its path there. The tags
        are the normal ones; where the file came from stays in the database (``details``), never
        in the file (ADR 0009). Paths where other tracks' files used to be are not reused."""
        cover = await self.services.cover(track.cover_url or result.cover_url)
        if cover is None and result.cover_url and track.cover_url:
            cover = await self.services.cover(result.cover_url)
        tags = tagging.Tags(
            title=track.title,
            artist=track.artist,
            album=track.album or result.album,
            isrc=track.isrc,
            mbid=track.mbid,
        )
        tagging.write_tags(result.path, tags, cover)
        relative = files.render_template(
            self.services.path_template,
            artist=track.artist,
            title=track.title,
            album=track.album or result.album,
            ext=result.format,
        )
        music_dir = self.services.config.music_dir
        with Session(self.engine) as session:
            former = session.exec(
                select(TrackFile.former_path).where(
                    TrackFile.former_path.is_not(None), TrackFile.track_id != track.id
                )
            ).all()
        saved = files.save_download(result.path, staging, music_dir, relative, taken=former)
        return saved.relative_to(Path(music_dir).resolve()).as_posix()

    async def scan_and_find(self) -> None:
        """Step 4: let Navidrome scan Releasomat's folder, then find the downloaded songs."""
        todo = [s for s in self.states if s.path and not s.song_id]
        if not todo:
            return
        self.step = "scan"
        self.log(
            f"{count(sum(s.new_download for s in todo), 'new download')}.",
            progress="Navidrome is scanning…",
        )
        navidrome = self.services.navidrome
        try:
            kind = await navidrome.scan()
            where = "Releasomat's folder" if kind == "folder" else "the whole library"
            self.log(f"Navidrome scanned {where}.")
        except NavidromeError as exc:
            # The songs may still show up (Navidrome's own watcher); look anyway.
            self.warnings.append(f"Navidrome's scan: {exc}")
            self.log(f"Navidrome's scan didn't work: {exc}")
        for state in todo:
            try:
                found = await navidrome.find_download(
                    state.track, state.path, self.services.threshold
                )
            except NavidromeError as exc:
                raise self.failed("scan", exc) from None
            if found is None:
                state.message = "Navidrome hasn't listed the file yet; it's added next time."
                self.save_file(state, error=state.message)
                continue
            state.song_id = found.id
            self.save_file(
                state, navidrome_song_id=found.id, error=None, details={"found_by": found.method}
            )
            self.track_event(state)

    async def update_playlist(self, snapshot: PlaylistSnapshot) -> str:
        """Step 5: the Navidrome playlist, in the source's order (missing tracks left out;
        adopted songs keep their place, ``held_id``). Returns the run's summary. From here on
        the refresh can't be stopped: saving the playlist is short, and Navidrome must never
        get half of it."""
        self.step = "playlist"
        if self.stop is not None:
            self.stop.too_late = True
        # Adopted songs keep their place while they wait in Tagwerk's inbox (hidden by Navidrome).
        song_ids = [s.song_id or s.held_id for s in self.states if s.song_id or s.held_id]
        summary = " ".join(p for p in (self.summary(), self.notes()) if p)
        problems = f" {' '.join(self.warnings)}" if self.warnings else ""
        if not song_ids:
            reason = "The playlist is empty" if not self.states else "None of its songs is"
            self.log(f"{reason} in Navidrome yet; the Navidrome playlist was left as it is.")
            return f"{summary} Navidrome playlist not changed: no songs to put in it yet.{problems}"
        self.log("", progress="Updating the Navidrome playlist…")
        with Session(self.engine) as session:
            playlist = session.get(Playlist, self.playlist_id)
            mode, stored_id = playlist.navidrome_mode, playlist.navidrome_playlist_id
            keep = playlist.snapshot_keep
        try:
            if mode == NavidromeMode.SNAPSHOT:
                navidrome_id, what = await self.save_snapshot(song_ids, keep)
            else:
                navidrome_id, what = await self.save_update(song_ids, stored_id)
            await self.check_playlist(navidrome_id, song_ids)
        except NavidromeError as exc:
            raise self.failed("playlist", exc) from None
        self.log(what)
        return f"{summary} {what}{problems}"

    async def save_update(self, song_ids: list[str], stored_id: str | None) -> tuple[str, str]:
        """Update mode: replace the songs of the same Navidrome playlist (its ID stays)."""
        navidrome = self.services.navidrome
        existing = await navidrome.get_playlist(stored_id) if stored_id else None
        if existing is not None:
            await navidrome.replace_playlist(stored_id, song_ids)
            renamed = await self.rename_dated(stored_id, existing.get("name") or "")
            return stored_id, f"Navidrome playlist updated ({self.songs(song_ids)}).{renamed}"
        new_id = await navidrome.create_playlist(self.title, song_ids)
        await self.comment(new_id, f"Kept up to date by Releasomat from {self.url}")
        with Session(self.engine) as session:
            session.get(Playlist, self.playlist_id).navidrome_playlist_id = new_id
            session.commit()
        again = " (the old one was gone)" if stored_id else ""
        return new_id, f"Navidrome playlist created{again} ({self.songs(song_ids)})."

    async def rename_dated(self, navidrome_id: str, name: str) -> str:
        """An older name with a date after a comma ("Weekly Exploration for rob, week of
        2026-10-19 Mon") becomes the current, undated one, once. Any other name stays: the owner
        may have renamed the playlist in Navidrome on purpose."""
        if name == self.title or not name.startswith(f"{self.title}, "):
            return ""
        try:
            await self.services.navidrome.update_playlist(navidrome_id, name=self.title)
        except NavidromeError as exc:  # only the name; never worth failing for
            log.info("Renaming Navidrome playlist %s failed: %s", navidrome_id, exc)
            return ""
        return f" Renamed to “{self.title}”."

    async def save_snapshot(self, song_ids: list[str], keep: int) -> tuple[str, str]:
        """Snapshot mode: a new Navidrome playlist named with the date. With
        SNAPSHOT_AUTO_DELETE on, only the newest ``keep`` that Releasomat made stay."""
        navidrome = self.services.navidrome
        day = utcnow().astimezone(self.services.config.zone).date().isoformat()
        with Session(self.engine) as session:
            names = set(
                session.exec(
                    select(NavidromeSnapshot.name).where(
                        NavidromeSnapshot.playlist_id == self.playlist_id
                    )
                ).all()
            )
        name = base = f"{self.title} {day}"
        number = 1
        while name in names:
            number += 1
            name = f"{base} ({number})"
        new_id = await navidrome.create_playlist(name, song_ids)
        await self.comment(new_id, f"Snapshot by Releasomat of {self.url}")
        with Session(self.engine) as session:
            session.add(
                NavidromeSnapshot(playlist_id=self.playlist_id, navidrome_id=new_id, name=name)
            )
            session.commit()
        what = f"Snapshot “{name}” created in Navidrome ({self.songs(song_ids)})."
        if self.services.snapshot_auto_delete:
            removed = await self.prune_snapshots(keep)
            if removed:
                what += f" {removed} older snapshot{'s' if removed != 1 else ''} deleted."
        return new_id, what

    async def prune_snapshots(self, keep: int) -> int:
        """Delete this playlist's oldest snapshot playlists beyond ``keep``: only the ones
        Releasomat made, found by their stored ID (never by name). Music files stay."""
        with Session(self.engine) as session:
            rows = session.exec(
                select(NavidromeSnapshot)
                .where(NavidromeSnapshot.playlist_id == self.playlist_id)
                .order_by(NavidromeSnapshot.created_at.desc(), NavidromeSnapshot.id.desc())
            ).all()
            old = [(row.id, row.navidrome_id) for row in rows[max(keep, 1) :]]
        removed = 0
        for row_id, navidrome_id in old:
            if await self.services.navidrome.get_playlist(navidrome_id) is not None:
                await self.services.navidrome.delete_playlist(navidrome_id)
                removed += 1
            with Session(self.engine) as session:
                session.delete(session.get(NavidromeSnapshot, row_id))
                session.commit()
        return removed

    async def comment(self, navidrome_id: str, text: str) -> None:
        try:
            await self.services.navidrome.update_playlist(navidrome_id, comment=text)
        except NavidromeError as exc:  # only a description; never worth failing for
            log.info("Playlist comment not set: %s", exc)

    async def check_playlist(self, navidrome_id: str, song_ids: list[str]) -> None:
        """Songs Navidrome left out of the playlist (deleted from the library since they were
        linked) are searched for again next time."""
        playlist = await self.services.navidrome.get_playlist(navidrome_id) or {}
        present = {entry.get("id") for entry in playlist.get("entry") or []}
        gone = [s for s in self.states if s.song_id and s.song_id not in present]
        if not gone or not present:
            return
        for state in gone:
            self.save_file(state, navidrome_song_id=None, forget_song=True)
        self.log(f"{len(gone)} linked song(s) no longer in Navidrome; they're looked for again.")

    def songs(self, song_ids: list[str]) -> str:
        """ "12 songs" in the Navidrome playlist; adopted songs that keep their place there are
        counted apart, since Navidrome hides them: "11 songs + 1 adopted song waiting for your
        library"."""
        held = sum(1 for s in self.states if s.held_id and not s.song_id)
        shown = count(len(song_ids) - held, "song")
        return (
            f"{shown} + {count(held, 'adopted song')} waiting for your library" if held else shown
        )

    def summary(self) -> str:
        """ "12 tracks: 5 in your library, 4 downloaded, 1 adopted (waiting for your library),
        1 Don't like, 1 missing, 0 failed." (adopted and Don't like only when there are some)"""
        counted = {"in_library": 0, "done": 0, "failed": 0, "adopted": 0, "disliked": 0}
        for state in self.states:
            if state.status in counted:
                counted[state.status] += 1
        new = sum(s.new_download for s in self.states)
        missing = len(self.states) - sum(counted.values())
        parts = [
            f"{counted['in_library']} in your library",
            f"{counted['done']} downloaded" + (f" ({new} new)" if new else ""),
        ]
        if counted["adopted"]:
            parts.append(f"{counted['adopted']} adopted (waiting for your library)")
        if counted["disliked"]:
            parts.append(f"{counted['disliked']} Don't like")
        parts += [f"{missing} missing", f"{counted['failed']} failed"]
        tracks = "1 track" if len(self.states) == 1 else f"{len(self.states)} tracks"
        return f"{tracks}: {', '.join(parts)}."

    # --- The track_file row of a track --------------------------------------------------------

    def save_file(
        self,
        state: TrackState,
        *,
        details: dict | None = None,
        attempt: bool = False,
        forget_song: bool = False,
        **values,
    ) -> None:
        """Create or update the track's ``track_file`` row (one per track). An adopted or
        "Don't like" row is left alone unless the caller sets its ``status``: those tracks are
        never downloaded, so nothing of a download may land on them by accident. A linked song
        that is gone from Navidrome (``forget_song``) leaves the track missing again, or
        adopted / "Don't like" as before (``lifecycle.status_after_song_gone``)."""
        with Session(self.engine) as session:
            file = file_of(session, state.track.id)
            if file is None:
                file = TrackFile(track_id=state.track.id)
                session.add(file)
            elif file.status in KEPT_AWAY and "status" not in values:
                return
            for key, value in values.items():
                setattr(file, key, value)
            if forget_song and file.status == FileStatus.IN_LIBRARY:
                file.status = lifecycle.status_after_song_gone(file)
            if details is not None:
                file.details = {**(file.details or {}), **details}
            if attempt:
                file.attempts += 1
            file.updated_at = utcnow()
            session.commit()


def count(number: int, thing: str) -> str:
    """ "1 track", "12 tracks"."""
    return f"{number} {thing}" if number == 1 else f"{number} {thing}s"


def _exists(music_dir: Path, file: TrackFile) -> bool:
    """Whether a downloaded file is still where Releasomat saved it."""
    try:
        return files.inside(music_dir, file.path).is_file()
    except files.UnsafePath:
        return False


def _has_files(folder: Path) -> bool:
    """Whether a folder exists and holds at least one file (somewhere inside). An unmounted
    share looks like a missing or empty folder."""
    if not folder.is_dir():
        return False
    return any(names for _root, _dirs, names in os.walk(folder))


def _name(state: TrackState) -> str:
    return f"{state.track.artist} – {state.track.title}"


def file_of(session: Session, track_id: int) -> TrackFile | None:
    return session.exec(
        select(TrackFile).where(TrackFile.track_id == track_id).order_by(TrackFile.id)
    ).first()


def upsert_track(session: Session, source: SourceTrack) -> Track:
    """The ``track`` row for a track from a source: the same one again when it has the same
    MusicBrainz ID, else the same ISRC, else the same main artist, song and version (and a
    similar length). New tracks are added; missing facts are filled in."""
    mbid = (source.mbid or "").strip().lower() or None
    isrc = (source.isrc or "").strip().upper().replace("-", "") or None
    key = matching.track_key(source.artist, source.title)
    track = None
    if mbid:
        track = session.exec(select(Track).where(Track.mbid == mbid)).first()
    if track is None and isrc:
        for candidate in session.exec(select(Track).where(Track.isrc == isrc)).all():
            if candidate.mbid in (None, mbid):
                track = candidate
                break
    if track is None:
        for candidate in session.exec(select(Track).where(Track.match_key == key)).all():
            if (
                candidate.mbid in (None, mbid)
                and candidate.isrc in (None, isrc)
                and matching.durations_agree(candidate.duration, source.duration)
            ):
                track = candidate
                break
    if track is None:
        track = Track(artist=source.artist, title=source.title)
        session.add(track)
    track.match_key = track.match_key or key
    track.mbid = track.mbid or mbid
    track.isrc = track.isrc or isrc
    track.album = track.album or source.album
    track.duration = track.duration or source.duration
    track.cover_url = track.cover_url or source.cover_url
    track.bpm = track.bpm or source.bpm or None
    track.key = track.key or (source.key or "").strip() or None
    session.flush()
    return track


INTERRUPTED_MESSAGE = "Interrupted: Releasomat stopped during this refresh."
INTERRUPTED_TRACK = (
    "Releasomat stopped while this track was downloading. The next refresh tries again."
)


def mark_interrupted(engine: Engine) -> int:
    """At startup: runs still marked "running" were cut off when Releasomat stopped. They end
    as failed, so nothing looks busy forever. Tracks stuck at "downloading" go back to
    "pending", saying why. Returns how many runs there were."""
    with Session(engine) as session:
        runs = session.exec(select(Run).where(Run.status == RunStatus.RUNNING.value)).all()
        for run in runs:
            run.status = RunStatus.FAILED.value
            run.message = INTERRUPTED_MESSAGE
            run.finished_at = run.finished_at or utcnow()
            run.add_log(INTERRUPTED_MESSAGE)
            playlist = session.get(Playlist, run.playlist_id)
            if playlist is not None and playlist.last_status == RunStatus.RUNNING:
                playlist.last_status = RunStatus.FAILED.value
        stuck = session.exec(
            select(TrackFile).where(
                TrackFile.status.in_([FileStatus.DOWNLOADING.value, FileStatus.SEARCHING.value])
            )
        ).all()
        for file in stuck:
            file.status, file.error = FileStatus.PENDING.value, INTERRUPTED_TRACK
        session.commit()
        return len(runs)


def _clock() -> str:
    return utcnow().strftime("%H:%M:%S UTC")
