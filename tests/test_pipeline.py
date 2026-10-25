"""A refresh end to end: read → match → download → scan → Navidrome playlist, with fakes for the
source, Navidrome and the download sources (tests never go online). Also: the download sources are
tried in the global order (ADR 0008), the worker never refreshes a playlist twice at the same time,
and no refresh ever deletes a file (ADR 0003)."""

import asyncio
import shutil
import time
from itertools import count
from pathlib import Path

import mutagen
import pytest
from sqlmodel import Session, select

from app import lifecycle, pipeline, settings
from app.downloaders.base import Downloader, DownloaderError, DownloadResult
from app.events import EventBus
from app.matching import match_song, track_key
from app.models import NavidromeSnapshot, Playlist, PlaylistTrack, Run, Track, TrackFile
from app.navidrome import FoundSong, NavidromeError
from app.pipeline import Services
from app.playlists import track_counts
from app.sources.base import PlaylistSnapshot, SourceError, SourceTrack
from app.tagging import read_tags
from app.worker import SKIPPED_MESSAGE, Worker

AUDIO = Path(__file__).parent / "fixtures" / "audio"
LIBRARY = [  # songs already in Navidrome (from the demo library)
    {
        "id": "bo",
        "artist": "Queen",
        "title": "Bohemian Rhapsody - Remastered 2011",
        "duration": 355,
    },
    {"id": "hb", "artist": "Beyoncé", "title": "Halo", "duration": 261},
    {"id": "hd", "artist": "Depeche Mode", "title": "Halo", "duration": 270},
]
QUEEN = SourceTrack("Queen", "Bohemian Rhapsody", album="A Night at the Opera", duration=354)
HALO = SourceTrack("Beyonce", "Halo", duration=261, isrc="USSM10804555")
EPLE = SourceTrack("Röyksopp", "Eple", album="Melody A.M.", duration=219, cover_url="https://c/1")
STROBE = SourceTrack("deadmau5", "Strobe (Extended Mix)", duration=637)


class FakeSource:
    """Answers each refresh with the next snapshot (or raises the next error)."""

    def __init__(self, *answers):
        self.answers = list(answers)

    async def fetch(self, ref):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeNavidrome:
    """Navidrome in memory: a library, a scan that picks up Releasomat's files (reading their
    tags; marked ``in_area``, and ``missing`` once their file is gone), playlists that silently
    drop unknown song IDs (like the real one). Like the real one, it keeps listing a deleted file
    until the next scan."""

    configured = True

    def __init__(self, music_dir: Path, library=LIBRARY):
        self.music_dir = music_dir
        self.library = {song["id"]: dict(song) for song in library}
        self.playlists: dict[str, dict] = {}
        self.calls: list[str] = []
        self.ids = count(1)
        self.fail: dict[str, Exception] = {}
        self.secrets = ["nd-password-1234"]

    def _call(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    @staticmethod
    def elsewhere(song) -> bool:
        """What the real one learns from Navidrome's own API (``song_info``): the file is in
        the library outside Releasomat's folder and not missing."""
        return not song.get("in_area") and not song.get("missing")

    async def find_in_library(self, track, threshold, skip_ids=()):
        self._call("find_in_library")
        songs = [s for s in self.library.values() if s["id"] not in skip_ids or self.elsewhere(s)]
        return match_song(track, songs, threshold)

    async def find_elsewhere(self, track, threshold, skip_ids=()):
        self._call("find_elsewhere")
        return match_song(track, [s for s in self.library.values() if self.elsewhere(s)], threshold)

    def add_song(self, song_id, path=None, *, duration=219, **tags):
        """A song elsewhere in the library (the owner moved or imported a file there)."""
        self.library[song_id] = {"id": song_id, "path": path, "duration": duration, **tags}

    async def scan(self):
        self._call("scan")
        known = {s.get("path") for s in self.library.values() if s.get("in_area")}
        for song in self.library.values():
            if song.get("in_area"):  # gone since the last scan: missing; back again: not
                song["missing"] = not (self.music_dir / song["path"]).is_file()
        for path in sorted(self.music_dir.rglob("*.opus")):
            relative = path.relative_to(self.music_dir).as_posix()
            if relative not in known:
                tags = read_tags(path)
                song_id = f"dl{next(self.ids)}"
                self.library[song_id] = {
                    "id": song_id, "path": relative, "duration": 2, "in_area": True, **tags
                }  # fmt: skip
        return "folder"

    async def find_download(self, track, relative_path, threshold):
        self._call("find_download")
        for song in self.library.values():
            if song.get("path") == relative_path:
                return FoundSong(song["id"], "path")
        return None

    async def get_playlist(self, playlist_id):
        self._call("get_playlist")
        playlist = self.playlists.get(playlist_id)
        if playlist is None:
            return None
        return {**playlist, "entry": [{"id": i} for i in playlist["songs"]]}

    async def create_playlist(self, name, song_ids):
        self._call("create_playlist")
        playlist_id = f"pl{next(self.ids)}"
        songs = [i for i in song_ids if i in self.library]
        self.playlists[playlist_id] = {"name": name, "songs": songs}
        return playlist_id

    async def replace_playlist(self, playlist_id, song_ids):
        self._call("replace_playlist")
        self.playlists[playlist_id]["songs"] = [i for i in song_ids if i in self.library]

    async def update_playlist(self, playlist_id, *, comment=None, name=None):
        self._call("update_playlist")
        if comment is not None:
            self.playlists[playlist_id]["comment"] = comment
        if name is not None:
            self.playlists[playlist_id]["name"] = name

    async def delete_playlist(self, playlist_id):
        self._call("delete_playlist")
        del self.playlists[playlist_id]


class FakeYouTube(Downloader):
    """Downloads the 2-second test tone for every track (or fails for ``fail`` titles)."""

    name = "youtube"
    label = "YouTube"

    def __init__(self, fail=()):
        self.fail = fail
        self.calls: list[str] = []

    async def download(self, track, staging_dir):
        self.calls.append(track.title)
        if track.title in self.fail:
            tried = [{"downloader": "youtube", "candidate": "YouTube Music: x", "reason": "live"}]
            raise DownloaderError("Nothing on YouTube looks like this track.", tried=tried)
        target = staging_dir / "abc123.opus"
        shutil.copy(AUDIO / "tone.opus", target)
        return DownloadResult(
            path=target,
            format="opus",
            bitrate=7,
            downloader="youtube",
            origin="https://music.youtube.com/watch?v=abc123",
            duration=2.0,
            album="YouTube Album",
        )


class FakeSoulseek(Downloader):
    """Soulseek without slskd: "delivers" the test tone, fails for ``fail`` titles, and is only
    connected when ``configured`` (like the real one with an address and API key)."""

    name = "slskd"
    label = "Soulseek (slskd)"

    def __init__(self, configured=True, fail=()):
        self.configured = configured
        self.fail = fail
        self.calls: list[str] = []

    async def download(self, track, staging_dir):
        self.calls.append(track.title)
        if track.title in self.fail:
            tried = [{"downloader": "slskd", "candidate": "user1: x.mp3", "reason": "128 kbit/s"}]
            raise DownloaderError("No file in the wanted formats on Soulseek.", tried=tried)
        target = staging_dir / "Eple.opus"
        shutil.copy(AUDIO / "tone.opus", target)
        return DownloadResult(
            path=target, format="opus", bitrate=320, downloader="slskd", origin="user1: Eple.flac"
        )


class RecordingSlskd(FakeSoulseek):
    """Stands in for SlskdDownloader in ``Services.from_settings`` (same arguments) and keeps
    what it was made with."""

    def __init__(
        self, url=None, api_key=None, *, downloads_dir=None, formats=None, min_bitrate=320,
        threshold=85,
    ):  # fmt: skip
        super().__init__(configured=bool(url and api_key))
        self.made_with = {
            "url": url,
            "api_key": api_key,
            "downloads_dir": downloads_dir,
            "formats": formats,
            "min_bitrate": min_bitrate,
            "threshold": threshold,
        }


async def fake_cover(url):
    return (AUDIO / "cover.jpg").read_bytes() if url else None


@pytest.fixture
def world(engine, config, monkeypatch):
    """A playlist, a fake source, a fake Navidrome and a fake YouTube, ready to refresh."""

    class World:
        source = FakeSource(PlaylistSnapshot("My Mix", [QUEEN, EPLE, HALO]))
        navidrome = FakeNavidrome(config.music_dir)
        youtube = FakeYouTube()
        events = EventBus()

        def services(self, **changes):
            values = {
                "navidrome": self.navidrome,
                "downloaders": [self.youtube],
                "config": config,
                "slots": asyncio.Semaphore(2),
                "secrets": ["nd-password-1234"],
                "cover": fake_cover,
                **changes,
            }
            return Services(**values)

        async def refresh(self, playlist_id=None, **changes) -> Run:
            run_id = await pipeline.refresh_playlist(
                playlist_id or self.playlist_id,
                "manual",
                engine=engine,
                events=self.events,
                services=self.services(**changes),
            )
            with Session(engine) as session:
                return session.get(Run, run_id)

        def playlist(self, playlist_id=None) -> Playlist:
            with Session(engine) as session:
                return session.get(Playlist, playlist_id or self.playlist_id)

        def files(self) -> dict[str, TrackFile]:
            with Session(engine) as session:
                rows = session.exec(select(TrackFile, Track).join(Track)).all()
                return {track.title: file for file, track in rows}

    world = World()
    world.playlist_id = _add(engine)
    monkeypatch.setattr(pipeline, "get_source", lambda source: world.source)
    return world


def _add(engine, source="deezer", source_id="908622995", **values) -> int:
    with Session(engine) as session:
        playlist = Playlist(source=source, source_id=source_id, url="https://x", title="Mix")
        for key, value in values.items():
            setattr(playlist, key, value)
        session.add(playlist)
        session.commit()
        return playlist.id


def music_files(config) -> list[str]:
    return sorted(p.relative_to(config.music_dir).as_posix() for p in config.music_dir.rglob("*.*"))


async def test_a_refresh_links_downloads_and_builds_the_playlist(world, config):
    seen = []

    async def listen():
        async for event in world.events.listen():
            seen.append(event)
            if event.type == "run" and event.data["status"] != "running":
                return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    run = await world.refresh()
    await asyncio.wait_for(listener, 1)

    assert run.status == "done", run.message
    assert run.message.startswith("3 tracks: 2 in your library, 1 downloaded (1 new), 0 missing")
    assert "Navidrome playlist created (3 songs)." in run.message
    assert (run.tracks_total, run.tracks_in_library, run.tracks_downloaded) == (3, 2, 1)
    assert run.report is None

    # The download: tagged, saved with the path template, nothing left in staging.
    assert music_files(config) == ["Röyksopp/Röyksopp - Eple.opus"]
    tags = read_tags(config.music_dir / "Röyksopp/Röyksopp - Eple.opus")
    assert tags == {"title": "Eple", "artist": "Röyksopp", "album": "Melody A.M.", "cover": "yes"}
    # No note of where it came from in the file (ADR 0009): that stays in the database.
    written = mutagen.File(config.music_dir / "Röyksopp/Röyksopp - Eple.opus").tags
    assert not [key for key, _value in written if "comment" in key.lower()]
    assert list((config.config_dir / "staging").iterdir()) == []

    # The Navidrome playlist, in the source's order.
    playlist = world.playlist()
    assert playlist.title == "My Mix"
    songs = world.navidrome.playlists[playlist.navidrome_playlist_id]
    assert songs["name"] == "My Mix" and songs["songs"] == ["bo", "dl1", "hb"]
    assert world.navidrome.calls.count("scan") == 1

    files = world.files()
    assert (files["Bohemian Rhapsody"].status, files["Bohemian Rhapsody"].path) == (
        "in_library",
        None,
    )
    assert files["Bohemian Rhapsody"].downloaded_by_releasomat is False  # never touched
    assert files["Halo"].details["matched_by"] == "name"
    eple = files["Eple"]
    assert (eple.status, eple.downloaded_by_releasomat, eple.downloader) == (
        "done",
        True,
        "youtube",
    )
    assert eple.navidrome_song_id == "dl1" and eple.format == "opus"
    assert eple.details["origin"] == "https://music.youtube.com/watch?v=abc123"

    statuses = [e.data["status"] for e in seen if e.type == f"track-{world.playlist_id}"]
    assert "downloading" in statuses and "done" in statuses and "in_library" in statuses
    assert [e.data["status"] for e in seen if e.type == "run"][-1] == "done"


async def test_refreshing_again_keeps_the_id_and_downloads_nothing_twice(world):
    first = await world.refresh()
    playlist_id = world.playlist().navidrome_playlist_id
    calls = len(world.navidrome.calls)
    second = await world.refresh()
    assert second.status == "done" and "Navidrome playlist updated (3 songs)." in second.message
    assert world.playlist().navidrome_playlist_id == playlist_id  # the same playlist
    assert world.youtube.calls == ["Eple"]  # downloaded once
    new_calls = world.navidrome.calls[calls:]
    assert new_calls == ["get_playlist", "replace_playlist", "get_playlist"]  # no search, no scan
    assert len(world.navidrome.playlists) == 1 and first.id != second.id


async def test_an_old_dated_name_is_renamed_once_other_names_stay(world):
    """Update mode used to keep ListenBrainz's first name ("…, week of 2026-10-12 Mon") forever,
    while the songs changed every week. A name like that becomes the undated one; a name the owner
    gave the playlist in Navidrome stays."""
    world.source.answers = [PlaylistSnapshot("Weekly Exploration for rob", [QUEEN, EPLE, HALO])]
    await world.refresh()
    navidrome_id = world.playlist().navidrome_playlist_id
    playlists = world.navidrome.playlists
    playlists[navidrome_id]["name"] = "Weekly Exploration for rob, week of 2026-10-12 Mon"

    run = await world.refresh()
    assert playlists[navidrome_id]["name"] == "Weekly Exploration for rob"
    assert "Renamed to “Weekly Exploration for rob”." in run.message
    assert world.playlist().navidrome_playlist_id == navidrome_id  # the same playlist

    playlists[navidrome_id]["name"] = "My DnB discoveries"  # renamed by the owner
    run = await world.refresh()
    assert playlists[navidrome_id]["name"] == "My DnB discoveries" and "Renamed" not in run.message


async def test_a_failed_read_changes_nothing_in_navidrome(world):
    await world.refresh()
    songs_before = dict(world.navidrome.playlists)
    calls = len(world.navidrome.calls)
    world.source.answers = [
        SourceError(
            "Deezer didn't answer (HTTP 503). The next refresh tries again.",
            url="https://api.deezer.com/playlist/908622995",
            http_status=503,
            excerpt='{"error": "Quota limit exceeded"}',
        )
    ]
    run = await world.refresh()
    assert run.status == "failed"
    assert run.message == "Deezer didn't answer (HTTP 503). The next refresh tries again."
    assert world.navidrome.calls[calls:] == []  # not a single call to Navidrome
    assert world.navidrome.playlists == songs_before
    report = run.report
    assert (report["step"], report["http_status"]) == ("read", 503)
    assert report["link"] == "https://api.deezer.com/playlist/908622995"
    assert report["excerpt"] == '{"error": "Quota limit exceeded"}'
    assert report["source"] == "Deezer" and report["versions"]["yt_dlp"]
    with Session(world_engine(world)) as session:  # the track list is the last good one
        assert len(session.exec(select(PlaylistTrack)).all()) == 3


def world_engine(world):
    from app.config import get_config
    from app.db import get_engine

    return get_engine(get_config().database_url)


async def test_unexpected_errors_end_as_failed_with_a_report(world):
    world.source.answers = [KeyError("oops")]
    run = await world.refresh()
    assert run.status == "failed" and run.message == "Unexpected error: KeyError: 'oops'"
    assert run.report["error_type"] == "KeyError" and "where" in run.report["details"]


async def test_tracks_that_leave_only_leave_the_navidrome_playlist(world, config):
    """ADR 0003: no refresh deletes a file."""
    await world.refresh()
    before = music_files(config)
    world.source.answers = [PlaylistSnapshot("My Mix", [HALO])]
    run = await world.refresh()
    assert run.status == "done" and run.message.startswith("1 track: 1 in your library")
    assert music_files(config) == before == ["Röyksopp/Röyksopp - Eple.opus"]
    playlist = world.playlist()
    assert world.navidrome.playlists[playlist.navidrome_playlist_id]["songs"] == ["hb"]
    with Session(world_engine(world)) as session:
        entries = session.exec(select(PlaylistTrack, Track).join(Track)).all()
        left = {track.title for entry, track in entries if entry.removed_at is not None}
    assert left == {"Bohemian Rhapsody", "Eple"}
    assert "2 left the playlist (their files stay)" in run.log

    world.source.answers = [PlaylistSnapshot("My Mix", [EPLE, HALO])]  # Eple is back
    run = await world.refresh()
    assert world.youtube.calls == ["Eple"]  # not downloaded again
    assert world.navidrome.playlists[playlist.navidrome_playlist_id]["songs"] == ["dl1", "hb"]


async def test_failed_downloads_are_in_the_report(world):
    """The report lists what each download source tried, in the order they were tried."""
    soulseek = FakeSoulseek(fail=("Eple",))
    world.youtube.fail = ("Eple",)
    run = await world.refresh(downloaders=[soulseek, world.youtube])
    assert run.status == "done" and run.tracks_failed == 1
    assert "0 downloaded, 0 missing, 1 failed" in run.message
    report = run.report
    assert report["message"] == "1 track couldn't be downloaded."
    track = report["tracks"][0]
    assert (track["position"], track["artist"], track["title"]) == (2, "Röyksopp", "Eple")
    assert track["message"] == (
        "Soulseek (slskd): No file in the wanted formats on Soulseek. / "
        "YouTube: Nothing on YouTube looks like this track."
    )
    reasons = [(t["downloader"], t["reason"]) for t in track["tried"]]
    assert reasons == [("slskd", "128 kbit/s"), ("youtube", "live")]
    eple = world.files()["Eple"]
    assert (eple.status, eple.attempts) == ("failed", 1)
    assert eple.error.startswith("Soulseek (slskd): No file in the wanted formats")
    # The Navidrome playlist still gets the two songs that are there.
    assert world.navidrome.playlists[world.playlist().navidrome_playlist_id]["songs"] == [
        "bo",
        "hb",
    ]


async def test_the_global_order_decides_which_source_is_tried_first(world):
    soulseek = FakeSoulseek()
    run = await world.refresh(downloaders=[soulseek, world.youtube])
    assert run.status == "done" and "1 downloaded (1 new)" in run.message
    assert (soulseek.calls, world.youtube.calls) == (["Eple"], [])
    eple = world.files()["Eple"]
    assert (eple.downloader, eple.details["origin"]) == ("slskd", "user1: Eple.flac")

    world.source.answers = [PlaylistSnapshot("My Mix", [STROBE])]  # a new missing track
    run = await world.refresh(downloaders=[world.youtube, soulseek])  # YouTube first now
    assert (soulseek.calls, world.youtube.calls) == (["Eple"], ["Strobe (Extended Mix)"])


async def test_the_next_source_is_tried_after_a_failure(world):
    soulseek = FakeSoulseek(fail=("Eple",))
    run = await world.refresh(downloaders=[soulseek, world.youtube])
    assert run.status == "done" and run.tracks_failed == 0 and run.report is None
    assert (soulseek.calls, world.youtube.calls) == (["Eple"], ["Eple"])
    eple = world.files()["Eple"]
    assert (eple.status, eple.downloader) == ("done", "youtube")


async def test_an_unconnected_soulseek_is_skipped_with_one_note(world):
    world.source.answers = [PlaylistSnapshot("My Mix", [QUEEN, EPLE, STROBE])]  # 2 to download
    soulseek = FakeSoulseek(configured=False)
    run = await world.refresh(downloaders=[soulseek, world.youtube])
    assert run.status == "done" and run.report is None
    assert run.tracks_downloaded == 2 and run.tracks_failed == 0
    assert soulseek.calls == [] and world.youtube.calls == ["Eple", "Strobe (Extended Mix)"]
    assert steps(run).count("Skipped, not connected: Soulseek (slskd)") == 1  # not one per track
    assert run.message.endswith("Skipped, not connected: Soulseek (slskd).")


async def test_no_connected_source_leaves_the_tracks_missing(world):
    run = await world.refresh(downloaders=[FakeSoulseek(configured=False)])
    assert run.status == "done" and run.report is None
    assert (run.tracks_missing, run.tracks_failed) == (1, 0)
    message = "No download source is available: Soulseek (slskd) isn't connected."
    assert message in run.message and steps(run).count(message) == 1
    eple = world.files()["Eple"]
    assert (eple.status, eple.error, eple.attempts) == ("skipped", message, 0)
    assert world.youtube.calls == []


async def test_no_source_switched_on_only_links(world):
    run = await world.refresh(downloaders=[])
    assert run.status == "done" and run.report is None and run.tracks_missing == 1
    assert "No download source is switched on: 1 missing track stays missing." in run.log
    assert world.youtube.calls == [] and music_files_of(world) == []
    playlist = world.playlist()
    assert world.navidrome.playlists[playlist.navidrome_playlist_id]["songs"] == ["bo", "hb"]


def steps(run: Run) -> str:
    """The run's log without its last line (the summary, which repeats the run's message)."""
    return "\n".join(run.log.splitlines()[:-1])


def music_files_of(world) -> list[str]:
    from app.config import get_config

    return music_files(get_config())


def test_services_follow_the_settings(session, config, monkeypatch):
    """DOWNLOAD_SOURCES decides which download sources a refresh uses, in which order; Soulseek
    gets its address, key, formats and minimum bitrate from the settings."""
    monkeypatch.setattr(pipeline, "SlskdDownloader", RecordingSlskd)
    names = [d.name for d in Services.from_settings(session, config).downloaders]
    assert names == ["slskd", "youtube"]  # the default
    soulseek = Services.from_settings(session, config).downloaders[0]
    assert soulseek.configured is False  # no address and API key yet: skipped in a refresh
    assert soulseek.made_with["formats"] == ["flac", "aiff", "wav", "mp3"]
    assert soulseek.made_with["min_bitrate"] == 320

    settings.save(session, "DOWNLOAD_SOURCES", "youtube,slskd")
    settings.save(session, "SLSKD_URL", "http://192.168.1.10:5030")
    settings.save(session, "SLSKD_API_KEY", "slskd-key-0042")
    settings.save(session, "SLSKD_FORMATS", "flac,mp3")
    settings.save(session, "SLSKD_MIN_BITRATE", "256")
    settings.save(session, "MATCH_THRESHOLD", "90")
    youtube, soulseek = Services.from_settings(session, config).downloaders
    assert youtube.name == "youtube" and soulseek.configured is True
    assert soulseek.made_with == {
        "url": "http://192.168.1.10:5030",
        "api_key": "slskd-key-0042",
        "downloads_dir": config.slskd_downloads_dir,
        "formats": ["flac", "mp3"],
        "min_bitrate": 256,
        "threshold": 90,
    }

    settings.save(session, "DOWNLOAD_SOURCES", "none")
    assert Services.from_settings(session, config).downloaders == []


async def test_navidrome_trouble_fails_the_run_before_downloading(world):
    world.navidrome.fail["find_in_library"] = NavidromeError(
        "No answer from http://nd:4533 in time.", call="search3"
    )
    run = await world.refresh()
    assert run.status == "failed"
    assert run.message == "Navidrome: No answer from http://nd:4533 in time."
    assert run.report["step"] == "match" and run.report["details"] == {"call": "search3"}
    assert world.youtube.calls == [] and world.navidrome.playlists == {}


async def test_secrets_never_reach_a_report(world):
    world.navidrome.fail["create_playlist"] = NavidromeError(
        "Navidrome refused.", call="createPlaylist", excerpt="login nd-password-1234 ?u=a&t=abc"
    )
    run = await world.refresh()
    assert run.status == "failed" and run.report["step"] == "playlist"
    assert "nd-password-1234" not in str(run.report) and "t=abc" not in str(run.report)


async def test_songs_gone_from_navidrome_are_looked_for_again(world):
    await world.refresh()
    del world.navidrome.library["hb"]  # the owner removed Halo from the library
    run = await world.refresh()
    assert "1 linked song(s) no longer in Navidrome" in run.log
    halo = world.files()["Halo"]
    assert (halo.status, halo.navidrome_song_id) == ("pending", None)
    world.youtube.fail = ("Halo",)
    run = await world.refresh()
    assert world.youtube.calls == ["Eple", "Halo"]  # searched again, then tried to download


# --- Files that leave Releasomat's folder (ADR 0009) ------------------------------------------

EPLE_FILE = "Röyksopp/Röyksopp - Eple.opus"
TWO_DOWNLOADS = PlaylistSnapshot("My Mix", [QUEEN, EPLE, STROBE])  # Eple and Strobe to download


def change(engine, title: str, action, *args):
    """A lifecycle change for one track, as Tagwerk's notices or the owner's buttons make it."""
    with Session(engine) as session:
        file = session.exec(select(TrackFile).join(Track).where(Track.title == title)).one()
        return action(session, file, *args)


def playlist_songs(world) -> list[str]:
    return world.navidrome.playlists[world.playlist().navidrome_playlist_id]["songs"]


async def test_a_file_deleted_by_hand_is_dont_like_and_not_downloaded_again(world, config):
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    assert playlist_songs(world) == ["bo", "dl1", "dl2"]
    (config.music_dir / EPLE_FILE).unlink()  # the owner, by hand; Navidrome still lists it

    run = await world.refresh()

    assert run.status == "done" and world.youtube.calls == ["Eple", "Strobe (Extended Mix)"]
    assert run.message.startswith(
        "3 tracks: 1 in your library, 1 downloaded, 1 Don't like, 0 missing, 0 failed. "
        "1 marked Don't like (its file was deleted from Releasomat's folder). "
        "Navidrome playlist updated (2 songs)."
    )
    assert (run.tracks_missing, run.tracks_downloaded) == (0, 1)
    eple = world.files()["Eple"]
    assert (eple.status, eple.disliked_reason, eple.path, eple.former_path) == (
        "disliked", "vanished", None, EPLE_FILE
    )  # fmt: skip
    assert eple.navidrome_song_id is None and eple.downloaded_by_releasomat is False
    # Navidrome's stale copy in Releasomat's folder is never linked again.
    assert playlist_songs(world) == ["bo", "dl2"]
    assert world.navidrome.calls.count("find_elsewhere") == 1

    run = await world.refresh()  # and stays so
    assert world.youtube.calls == ["Eple", "Strobe (Extended Mix)"]
    assert world.files()["Eple"].status == "disliked" and "marked" not in run.message


async def test_a_file_moved_into_the_library_is_linked_there(world, config):
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    (config.music_dir / EPLE_FILE).unlink()  # the owner moved it into the library, which
    world.navidrome.add_song("lib-eple", "Demo/Röyksopp/Eple.opus", title="Eple",
                             artist="Röyksopp")  # fmt: skip

    run = await world.refresh()

    assert "3 tracks: 2 in your library, 1 downloaded, 0 missing, 0 failed." in run.message
    assert "1 moved into your library." in run.message
    eple = world.files()["Eple"]
    assert (eple.status, eple.navidrome_song_id, eple.path, eple.former_path) == (
        "in_library", "lib-eple", None, EPLE_FILE
    )  # fmt: skip
    assert eple.details["moved"] is True and eple.disliked_at is None
    assert playlist_songs(world) == ["bo", "lib-eple", "dl2"]  # the library copy
    assert world.youtube.calls == ["Eple", "Strobe (Extended Mix)"]


async def test_navidrome_keeping_the_id_of_a_moved_file_is_fine(world, config):
    """Navidrome notices a move during its scan and keeps the song's ID (seen with the dev
    Navidrome 0.64): the old ID is linked again, now as the library copy."""
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    (config.music_dir / EPLE_FILE).unlink()
    song = world.navidrome.library["dl1"]
    song["in_area"], song["path"] = False, "Demo/Röyksopp/Röyksopp - Eple.opus"  # its scan
    song["duration"] = 219  # as long as the real song (the test tone is 2 s)

    run = await world.refresh()

    assert "1 moved into your library." in run.message
    eple = world.files()["Eple"]
    assert (eple.status, eple.navidrome_song_id, eple.downloaded_by_releasomat) == (
        "in_library", "dl1", False
    )  # fmt: skip
    assert playlist_songs(world) == ["bo", "dl1", "dl2"]


async def test_a_file_moved_before_navidrome_scanned_is_lifted_later(world, config):
    """The guessed Don't like ends by itself when the song turns up in the library."""
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    (config.music_dir / EPLE_FILE).unlink()
    await world.refresh()
    assert world.files()["Eple"].disliked_reason == "vanished"
    world.navidrome.add_song("lib-eple", title="Eple", artist="Röyksopp")  # scanned now

    run = await world.refresh()

    assert "1 moved into your library." in run.message and "Don't like" not in run.message
    eple = world.files()["Eple"]
    assert (eple.status, eple.disliked_at, eple.disliked_reason) == ("in_library", None, None)
    assert playlist_songs(world) == ["bo", "lib-eple", "dl2"]


async def test_an_unmounted_folder_marks_nothing(world, config):
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    hidden = config.music_dir.with_name("music-unmounted")
    config.music_dir.rename(hidden)  # the share isn't mounted

    run = await world.refresh()

    assert run.status == "done"
    assert "Releasomat's folder looks unmounted; nothing was marked Don't like." in run.message
    assert "2 downloaded" in run.message and "marked Don't like (" not in run.message
    files = world.files()
    assert {files[t].status for t in ("Eple", "Strobe (Extended Mix)")} == {"done"}
    assert files["Eple"].path == EPLE_FILE and files["Eple"].disliked_at is None
    assert world.navidrome.calls.count("find_elsewhere") == 0
    assert playlist_songs(world) == ["bo", "dl1", "dl2"]  # left as it was

    config.music_dir.mkdir()  # mounted, but empty: the same
    run = await world.refresh()
    assert "looks unmounted" in run.message and world.files()["Eple"].status == "done"
    config.music_dir.rmdir()
    hidden.rename(config.music_dir)  # back: all fine, nothing happened
    run = await world.refresh()
    assert "unmounted" not in run.message and world.files()["Eple"].status == "done"


async def test_every_file_of_a_playlist_gone_at_once_is_a_brake(world, config):
    tracks = [SourceTrack("Artist", f"Song {n}", duration=2) for n in range(1, 6)]
    world.source.answers = [PlaylistSnapshot("Mix", tracks)]
    await world.refresh()
    (config.music_dir / "Other").mkdir()
    (config.music_dir / "Other/Other - Song.opus").write_bytes(b"another playlist's file")
    for path in config.music_dir.glob("Artist/*.opus"):
        path.unlink()

    run = await world.refresh()
    assert "looks unmounted" in run.message
    assert {f.status for f in world.files().values()} == {"done"}

    (config.music_dir / "Artist/Artist - Song 5.opus").write_bytes(b"x")  # 4 of 5 gone: decided
    run = await world.refresh()
    assert "4 marked Don't like (their files were deleted" in run.message
    assert [f.status for f in world.files().values()].count("disliked") == 4


async def test_adopted_tracks_are_never_downloaded_and_linked_once_imported(world, config, engine):
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    assert change(engine, "Eple", lifecycle.adopt).changed  # Tagwerk's adopt notice
    (config.music_dir / EPLE_FILE).unlink()  # then it moves the file into its inbox

    run = await world.refresh()

    assert run.message.startswith(
        "3 tracks: 1 in your library, 1 downloaded, 1 adopted (waiting for your library), "
        "0 missing, 0 failed."
    )
    assert "Don't like" not in run.message and run.tracks_missing == 0
    assert world.youtube.calls == ["Eple", "Strobe (Extended Mix)"]
    assert world.files()["Eple"].status == "adopted"
    with Session(engine) as session:
        counts = track_counts(session, world.playlist_id)
    assert counts == {
        "total": 3, "in_library": 1, "downloaded": 1, "failed": 0, "adopted": 1, "disliked": 0,
        "missing": 0,
    }  # fmt: skip
    assert playlist_songs(world) == ["bo", "dl2"]  # missing while it waits in the inbox

    world.navidrome.add_song(
        "lib-eple", "Röyksopp/Melody A.M./02 Eple.flac", title="Eple", artist="Röyksopp"
    )  # Tagwerk imported it; fmt: skip
    run = await world.refresh()
    assert "3 tracks: 2 in your library" in run.message
    assert "1 adopted into your library." in run.message
    eple = world.files()["Eple"]
    assert (eple.status, eple.navidrome_song_id) == ("in_library", "lib-eple")
    assert eple.adopted_at is not None
    assert playlist_songs(world) == ["bo", "lib-eple", "dl2"]

    del world.navidrome.library["lib-eple"]  # the owner deleted it from the library later
    run = await world.refresh()
    assert world.files()["Eple"].status == "adopted"  # not missing: never downloaded again
    await world.refresh()
    assert world.youtube.calls == ["Eple", "Strobe (Extended Mix)"]


async def test_dont_like_is_not_downloaded_but_linked_from_the_library(world, config, engine):
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    deleted, result = change(engine, "Eple", lifecycle.delete_and_dislike, config.music_dir)
    assert result.changed and not (config.music_dir / EPLE_FILE).exists()

    run = await world.refresh()
    assert "1 Don't like" in run.message and world.files()["Eple"].status == "disliked"
    assert world.youtube.calls == ["Eple", "Strobe (Extended Mix)"]

    world.navidrome.add_song("lib-eple", title="Eple", artist="Röyksopp")  # the owner has it
    run = await world.refresh()
    eple = world.files()["Eple"]
    assert (eple.status, eple.disliked_reason) == ("in_library", "deleted")  # still Don't like
    assert playlist_songs(world) == ["bo", "lib-eple", "dl2"]
    assert "Don't like, but in your library: 1 track linked." in run.log


# Two tracks as long as the test tone: Navidrome's copies of their downloads match them by name.
SHORT = PlaylistSnapshot(
    "Mix", [SourceTrack("Bicep", "Glue", duration=2), SourceTrack("Moby", "Porcelain", duration=2)]
)
GLUE_FILE = "Bicep/Bicep - Glue.opus"


async def test_navidromes_stale_copy_in_the_folder_is_never_linked(world, config):
    """Until its next scan Navidrome still lists a deleted file, and that song matches the track
    by name: it must not be taken for a copy elsewhere in the library."""
    world.source.answers = [SHORT]
    await world.refresh()
    (config.music_dir / GLUE_FILE).unlink()
    assert not world.navidrome.library["dl1"].get("missing")  # Navidrome hasn't noticed

    run = await world.refresh()

    assert "1 marked Don't like" in run.message
    assert world.files()["Glue"].status == "disliked" and playlist_songs(world) == ["dl2"]


async def test_undo_downloads_it_again(world, config, engine):
    world.source.answers = [SHORT]
    await world.refresh()
    change(engine, "Glue", lifecycle.delete_and_dislike, config.music_dir)
    await world.refresh()
    assert change(engine, "Glue", lifecycle.undo_dislike).changed

    run = await world.refresh()

    # Downloaded again, not linked to Navidrome's stale copy of the deleted file.
    assert world.youtube.calls == ["Glue", "Porcelain", "Glue"]
    assert "2 downloaded (1 new)" in run.message
    song = world.files()["Glue"]
    assert (song.status, song.path, song.downloaded_by_releasomat) == ("done", GLUE_FILE, True)
    assert (config.music_dir / GLUE_FILE).exists()
    assert playlist_songs(world) == ["dl1", "dl2"]  # Navidrome found the file at its old path


async def test_an_adopt_notice_during_a_refresh_wins(world, config, engine):
    """The refresh sees the file gone; Tagwerk's adopt notice comes in while it looks for the
    song: the track ends adopted, not Don't like."""
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    (config.music_dir / EPLE_FILE).unlink()
    find_elsewhere = world.navidrome.find_elsewhere

    async def notice_meanwhile(track, threshold, skip_ids=()):
        if track.title == "Eple" and world.files()["Eple"].status == "done":
            with Session(engine) as session:  # what the integration API does
                file = session.get(TrackFile, world.files()["Eple"].id)
                file.status, file.former_path, file.path = "adopted", file.path, None
                file.downloaded_by_releasomat = False
                session.commit()
        return await find_elsewhere(track, threshold, skip_ids)

    world.navidrome.find_elsewhere = notice_meanwhile
    run = await world.refresh()
    assert world.files()["Eple"].status == "adopted" and "marked Don't like" not in run.message
    assert "1 adopted (waiting for your library)" in run.message


async def test_adopted_rows_are_left_alone_by_downloads(world, engine):
    """``save_file`` never changes an adopted or Don't like row unless it sets the status."""
    world.source.answers = [TWO_DOWNLOADS]
    await world.refresh()
    change(engine, "Eple", lifecycle.adopt)
    with Session(engine) as session:
        refresh = pipeline.Refresh(engine, world.events, world.services(), world.playlist_id, 1)
        track = session.exec(select(Track).where(Track.title == "Eple")).one()
    state = pipeline.TrackState(1, track)
    refresh.save_file(state, error="Navidrome hasn't listed the file yet", path="x.opus")
    eple = world.files()["Eple"]
    assert (eple.status, eple.path, eple.error) == ("adopted", None, None)


async def test_bpm_and_key_are_stored(world, engine):
    world.source.answers = [
        PlaylistSnapshot(
            "Mix", [SourceTrack("Bicep", "Glue", duration=269, bpm=130.0, key="Gb Minor")]
        )  # fmt: skip
    ]
    await world.refresh()
    world.source.answers = [PlaylistSnapshot("Mix", [SourceTrack("Bicep", "Glue", duration=269)])]
    await world.refresh()  # a source without them doesn't erase them
    with Session(engine) as session:
        track = session.exec(select(Track).where(Track.title == "Glue")).one()
    assert (track.bpm, track.key) == (130.0, "Gb Minor")


async def test_a_track_shared_by_two_playlists_is_downloaded_once(world, engine):
    await world.refresh()
    other = _add(engine, source_id="1111")
    world.source.answers = [PlaylistSnapshot("Other", [SourceTrack("Röyksopp", "Eple", None, 218)])]
    run = await world.refresh(other)
    assert run.status == "done" and world.youtube.calls == ["Eple"]
    assert world.navidrome.playlists[world.playlist(other).navidrome_playlist_id]["songs"] == [
        "dl1"
    ]


async def test_snapshots_stay_unless_automatic_deleting_is_on(world):
    with Session(world_engine(world)) as session:
        playlist = session.get(Playlist, world.playlist_id)
        playlist.navidrome_mode, playlist.snapshot_keep = "snapshot", 2
        session.commit()
    world.navidrome.playlists["mine"] = {"name": "My Mix", "songs": ["bo"]}  # the owner's own
    for _ in range(3):
        run = await world.refresh()
        assert run.status == "done", run.message
    names = sorted(p["name"] for p in world.navidrome.playlists.values())
    day = names[1].removeprefix("My Mix ")
    assert names == ["My Mix", f"My Mix {day}", f"My Mix {day} (2)", f"My Mix {day} (3)"]
    assert "delete_playlist" not in world.navidrome.calls  # off: nothing is ever deleted

    world.navidrome.playlists["mine2"] = {"name": f"My Mix {day}", "songs": []}  # same name
    run = await world.refresh(snapshot_auto_delete=True)
    assert "2 older snapshots deleted." in run.message
    names = sorted(p["name"] for p in world.navidrome.playlists.values())
    assert names == ["My Mix", f"My Mix {day}", f"My Mix {day} (3)", f"My Mix {day} (4)"]
    assert "mine" in world.navidrome.playlists and "mine2" in world.navidrome.playlists
    with Session(world_engine(world)) as session:
        kept = session.exec(select(NavidromeSnapshot.name)).all()
    assert sorted(kept) == [f"My Mix {day} (3)", f"My Mix {day} (4)"]
    assert world.playlist().navidrome_playlist_id is None  # update mode's playlist untouched


async def test_a_missing_playlist_is_no_error(engine):
    assert await pipeline.refresh_playlist(999, "manual", engine=engine) is None


def test_runs_cut_off_by_a_restart_end_as_failed(engine):
    playlist_id = _add(engine)
    with Session(engine) as session:
        session.add(Run(playlist_id=playlist_id, trigger="schedule", status="running"))
        session.get(Playlist, playlist_id).last_status = "running"
        track = Track(artist="A", title="B")
        session.add(track)
        session.commit()
        session.add(TrackFile(track_id=track.id, status="downloading"))
        session.commit()
    assert pipeline.mark_interrupted(engine) == 1
    with Session(engine) as session:
        run = session.exec(select(Run)).one()
        assert run.status == "failed" and "Interrupted" in run.message
        assert session.get(Playlist, playlist_id).last_status == "failed"
        assert session.exec(select(TrackFile)).one().status == "pending"


async def test_a_playlist_never_refreshes_twice_at_once(engine):
    playlist_id = _add(engine)
    other_id = _add(engine, source_id="1111111111111111111111")
    started, release = [], asyncio.Event()

    async def slow_refresh(pid, trigger, stop):
        started.append((pid, trigger))
        await release.wait()

    worker = Worker(engine, refresh=slow_refresh, events=EventBus())
    worker.start()
    try:
        assert worker.request_refresh(playlist_id, "manual") is True
        assert worker.state(playlist_id) == "waiting"
        await asyncio.sleep(0.01)
        assert worker.state(playlist_id) == "running"
        assert worker.request_refresh(playlist_id, "manual") is False  # already refreshing
        assert worker.request_refresh(playlist_id, "schedule") is False  # recorded as skipped
        assert worker.request_refresh(other_id, "manual") is True  # others are not held up
        release.set()
        await asyncio.wait_for(worker.join(), 1)
        assert started == [(playlist_id, "manual"), (other_id, "manual")]
        assert worker.state(playlist_id) is None
        assert worker.request_refresh(playlist_id, "manual") is True  # free again
        await asyncio.wait_for(worker.join(), 1)
    finally:
        await worker.stop()

    with Session(engine) as session:
        runs = session.exec(select(Run)).all()
    assert [(r.status, r.trigger, r.message) for r in runs] == [
        ("skipped", "schedule", SKIPPED_MESSAGE)
    ]


def test_refresh_now_through_the_api_shows_the_report(client, monkeypatch):
    """Through the real worker and settings: without Navidrome settings the run fails after
    reading, with a report and a Copy report button."""
    monkeypatch.setattr(pipeline, "get_source", lambda s: FakeSource(PlaylistSnapshot("T", [EPLE])))
    monkeypatch.setattr(pipeline, "SlskdDownloader", RecordingSlskd)
    playlist = client.post(
        "/api/playlists", json={"url": "https://www.deezer.com/de/playlist/908622995"}
    ).json()
    response = client.post(f"/api/playlists/{playlist['id']}/refresh")
    assert response.status_code == 202 and response.json()["queued"] is True

    for _ in range(200):  # the worker runs in the app's event loop; give it a moment
        runs = client.get("/api/runs", params={"playlist_id": playlist["id"]}).json()
        if runs and runs[0]["status"] != "running":
            break
        time.sleep(0.02)
    run = runs[0]
    assert run["status"] == "failed" and run["trigger"] == "manual"
    assert run["message"].startswith("Navidrome isn't set up")
    assert run["report"]["step"] == "match"
    text = client.get(f"/api/runs/{run['id']}/report").text
    assert text.startswith("Releasomat error report") and "Navidrome isn't set up" in text
    for page in ("/activity", f"/playlists/{playlist['id']}"):
        html = client.get(page).text
        assert "Copy report" in html and "Navidrome isn&#39;t set up" in html, page
    tracks = client.get(f"/api/playlists/{playlist['id']}/tracks").json()
    assert [(t["title"], t["status"]) for t in tracks] == [("Eple", "pending")]
    assert client.get("/api/runs/999/report").status_code == 404
    assert client.post("/api/playlists/999/refresh").status_code == 404


def test_snapshot_auto_delete_is_a_switch_in_settings(client):
    assert settings.BY_KEY["SNAPSHOT_AUTO_DELETE"].default == "false"
    page = client.get("/settings").text
    assert 'name="SNAPSHOT_AUTO_DELETE"' in page and "Music files are never deleted" in page
    client.post(
        "/settings", data={"shown_SNAPSHOT_AUTO_DELETE": "1", "SNAPSHOT_AUTO_DELETE": "true"}
    )
    views = {v["key"]: v for v in client.get("/api/settings").json()}
    assert views["SNAPSHOT_AUTO_DELETE"]["value"] == "true"
    client.post("/settings", data={"shown_SNAPSHOT_AUTO_DELETE": "1"})  # switched off
    views = {v["key"]: v for v in client.get("/api/settings").json()}
    assert views["SNAPSHOT_AUTO_DELETE"]["value"] == "false"


async def test_a_track_another_refresh_is_downloading_is_not_downloaded_twice(world):
    with Session(world_engine(world)) as session:
        track = Track(artist="Röyksopp", title="Eple", duration=219)
        track.match_key = track_key(track.artist, track.title)  # the same track as EPLE
        session.add(track)
        session.commit()
        session.add(TrackFile(track_id=track.id, status="downloading", downloader="youtube"))
        session.commit()
        track_id = track.id
    # Another refresh is downloading it right now.
    other = pipeline.DOWNLOADING_NOW[track_id] = asyncio.Event()

    async def other_refresh_finishes():
        await asyncio.sleep(0.05)
        with Session(world_engine(world)) as session:
            row = session.exec(select(TrackFile).where(TrackFile.track_id == track_id)).one()
            row.status, row.path, row.downloaded_by_releasomat = "done", "R/R - Eple.opus", True
            session.commit()
        pipeline.DOWNLOADING_NOW.pop(track_id)
        other.set()

    finisher = asyncio.create_task(other_refresh_finishes())
    run = await world.refresh()
    await finisher
    assert world.youtube.calls == []  # the other refresh's file is used
    assert run.status == "done" and "1 downloaded, 0 missing" in run.message


async def test_a_left_over_downloading_mark_doesnt_hold_up_a_refresh(world):
    """A "downloading" status in the database that no running download belongs to (left by an
    older version after a stop) must not make a refresh wait for it."""
    with Session(world_engine(world)) as session:
        track = Track(artist="Röyksopp", title="Eple", duration=219)
        track.match_key = track_key(track.artist, track.title)
        session.add(track)
        session.commit()
        session.add(TrackFile(track_id=track.id, status="downloading", downloader="slskd"))
        session.commit()
    run = await asyncio.wait_for(world.refresh(), 5)
    assert run.status == "done" and world.youtube.calls == ["Eple"]
    assert world.files()["Eple"].status == "done"


async def test_each_track_tries_every_source_before_the_next_track_starts(world):
    """With Soulseek first and nothing found there, YouTube follows right away for the same
    track, not after every other track's Soulseek search."""
    order = []

    class Soulseek(FakeSoulseek):
        async def download(self, track, staging_dir):
            order.append(("slskd", track.title))
            return await super().download(track, staging_dir)

    class YouTube(FakeYouTube):
        async def download(self, track, staging_dir):
            order.append(("youtube", track.title))
            return await super().download(track, staging_dir)

    tracks = [SourceTrack("Artist", f"Song {n}", duration=2) for n in (1, 2, 3)]
    world.source.answers = [PlaylistSnapshot("Mix", tracks)]
    titles = [t.title for t in tracks]
    run = await world.refresh(
        downloaders=[Soulseek(fail=titles), YouTube()], slots=asyncio.Semaphore(1)
    )
    assert run.status == "done", run.message
    assert order == [(source, title) for title in titles for source in ("slskd", "youtube")]
