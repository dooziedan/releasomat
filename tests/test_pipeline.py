"""A refresh end to end: read → match → download → scan → Navidrome playlist, with fakes for the
source, Navidrome and the download sources (tests never go online). Also: the download sources are
tried in the global order (ADR 0008), the worker never refreshes a playlist twice at the same time,
and no refresh ever deletes a file (ADR 0003)."""

import asyncio
import shutil
import time
from itertools import count
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app import pipeline, settings
from app.downloaders.base import Downloader, DownloaderError, DownloadResult
from app.events import EventBus
from app.matching import match_song, track_key
from app.models import NavidromeSnapshot, Playlist, PlaylistTrack, Run, Track, TrackFile
from app.navidrome import FoundSong, NavidromeError
from app.pipeline import Services
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
    tags), playlists that silently drop unknown song IDs (like the real one)."""

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

    async def find_in_library(self, track, threshold):
        self._call("find_in_library")
        return match_song(track, list(self.library.values()), threshold)

    async def scan(self):
        self._call("scan")
        known = {s.get("path") for s in self.library.values()}
        for path in sorted(self.music_dir.rglob("*.opus")):
            relative = path.relative_to(self.music_dir).as_posix()
            if relative not in known:
                tags = read_tags(path)
                song_id = f"dl{next(self.ids)}"
                self.library[song_id] = {"id": song_id, "path": relative, "duration": 2, **tags}
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


async def test_a_file_deleted_by_hand_is_downloaded_again(world, config):
    await world.refresh()
    (config.music_dir / "Röyksopp/Röyksopp - Eple.opus").unlink()  # the owner, by hand
    run = await world.refresh()
    assert run.status == "done" and world.youtube.calls == ["Eple", "Eple"]
    assert music_files(config) == ["Röyksopp/Röyksopp - Eple.opus"]


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
