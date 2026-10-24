"""The YouTube downloader: search, checks, download, the worker process. ytmusicapi and yt-dlp are
replaced by fakes: tests never go online."""

import io
import json
import shutil
import sys
from pathlib import Path

import pytest

from app import files
from app.downloaders import youtube, youtube_job
from app.downloaders.base import DownloaderError
from app.downloaders.youtube import WorkerFailed, YouTubeDownloader, run_in_fresh_process
from app.downloaders.youtube_job import explain, run
from app.sources.base import SourceTrack

TONE = Path(__file__).parent / "fixtures" / "audio" / "tone.opus"  # 2 seconds long


def ytm_song(video_id, title, artist, seconds, album="Album", available=True):
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"name": artist}],
        "duration_seconds": seconds,
        "album": {"name": album},
        "isAvailable": available,
        "thumbnails": [{"url": "https://lh3.googleusercontent.com/x=w60-h60-l90-rj"}],
    }


class FakeYTMusic:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.queries = results or [], error, []

    def search(self, query, filter=None, limit=None):
        assert filter == "songs"
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results


class FakeYoutubeDL:
    """Answers searches with ``videos`` and "downloads" by copying the 2-second tone (or
    ``files[id]``) into the staging folder. ``broken`` IDs fail like yt-dlp does."""

    def __init__(self, videos=(), files=None, broken=()):
        self.videos, self.files, self.broken = list(videos), files or {}, broken
        self.options = []

    def __call__(self, options):
        self.options.append(options)
        self.current = options
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if url.startswith("ytsearch5:"):
            return {"entries": self.videos}
        video_id = url.rsplit("=", 1)[1]
        if video_id in self.broken:
            raise RuntimeError(f"ERROR: [youtube] {video_id}: {self.broken[video_id]}")
        target = Path(self.current["paths"]["home"]) / f"{video_id}.opus"
        shutil.copy(self.files.get(video_id, TONE), target)
        return {"requested_downloads": [{"filepath": str(target)}]}


@pytest.fixture
def job(tmp_path):
    config = tmp_path / "config"
    staging = files.new_staging_dir(config)
    return {
        "artist": "Daft Punk",
        "title": "Get Lucky",
        "album": None,
        "duration": 5,
        "isrc": None,
        "mbid": None,
        "staging_dir": str(staging),
        "config_dir": str(config),
        "threshold": 85,
        "pause": 0,
    }


def test_the_best_youtube_music_song_is_downloaded(job):
    ytmusic = FakeYTMusic(
        [
            ytm_song("live", "Get Lucky (Live)", "Daft Punk", 6),
            ytm_song("long", "Get Lucky", "Daft Punk", 369),
            ytm_song("other", "Happy", "Pharrell Williams", 4),
            ytm_song("gone", "Get Lucky", "Daft Punk", 4, available=False),
            ytm_song("good", "Get Lucky", "Daft Punk", 4, album="Random Access Memories"),
        ]
    )
    ydl = FakeYoutubeDL()
    result = run(job, ytmusic=ytmusic, youtube_dl=ydl)
    assert result["ok"] is True
    assert result["origin"] == "https://music.youtube.com/watch?v=good"
    assert result["format"] == "opus" and result["album"] == "Random Access Memories"
    assert Path(result["path"]).parent == Path(job["staging_dir"])
    assert result["cover_url"] == "https://lh3.googleusercontent.com/x=w544-h544-l90-rj"
    assert ytmusic.queries == ["daft punk get lucky"]
    reasons = {t["candidate"].split(" ")[-1]: t["reason"] for t in result["tried"]}
    assert reasons["https://music.youtube.com/watch?v=long"].startswith("different length: 6:09")
    assert reasons["https://music.youtube.com/watch?v=live"] == "another version (live)"
    assert "don't match" in reasons["https://music.youtube.com/watch?v=other"]
    assert reasons["https://music.youtube.com/watch?v=gone"] == "not available"
    options = ydl.options[-1]
    assert options["format"] == "bestaudio/best"  # the original stream, no re-encoding
    assert options["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}]
    assert options["cachedir"].startswith(job["config_dir"])
    assert "cookiefile" not in options


def test_a_file_of_the_wrong_length_is_discarded_and_the_next_one_tried(job):
    ytmusic = FakeYTMusic(
        [ytm_song("a", "Get Lucky", "Daft Punk", 8), ytm_song("b", "Get Lucky", "Daft Punk", 9)]
    )
    job["duration"] = 15  # the search results look right, but the tone is only 2 s long
    result = run(job, ytmusic=ytmusic, youtube_dl=FakeYoutubeDL())
    assert result["ok"] is False
    assert result["message"] == (
        "2 download(s) tried, none worked: the file is 0:02 long instead of 0:15"
    )
    assert [t["candidate"].split(" ")[-1][-1] for t in result["tried"]] == ["a", "b"]
    assert list(Path(job["staging_dir"]).iterdir()) == []  # wrong files are not kept


def test_youtube_videos_when_youtube_music_has_nothing(job):
    videos = [
        {"id": "cover", "title": "Get Lucky - Daft Punk (cover)", "duration": 4, "channel": "x"},
        {"id": "vid", "title": "Daft Punk - Get Lucky (Official Audio)", "duration": 4,
         "channel": "Daft Punk"},
    ]  # fmt: skip
    ydl = FakeYoutubeDL(videos)
    result = run(job, ytmusic=FakeYTMusic([]), youtube_dl=ydl)
    assert result["ok"] and result["origin"] == "https://www.youtube.com/watch?v=vid"
    assert result["cover_url"] == "https://i.ytimg.com/vi/vid/hqdefault.jpg"
    assert result["tried"][0]["reason"] == "another version (cover)"


def test_cookies_are_used_as_a_copy(job):
    cookies = Path(job["config_dir"]) / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    ydl = FakeYoutubeDL()
    run(job, ytmusic=FakeYTMusic([ytm_song("a", "Get Lucky", "Daft Punk", 4)]), youtube_dl=ydl)
    used = Path(ydl.options[-1]["cookiefile"])
    assert used.parent == Path(job["staging_dir"]) and used != cookies


def test_failures_explain_themselves(job):
    ydl = FakeYoutubeDL(broken={"a": "Sign in to confirm you're not a bot"})
    result = run(
        job, ytmusic=FakeYTMusic([ytm_song("a", "Get Lucky", "Daft Punk", 4)]), youtube_dl=ydl
    )
    assert "confirm this isn't a bot" in result["message"]
    result = run(
        job, ytmusic=FakeYTMusic(error=ConnectionError("down")), youtube_dl=FakeYoutubeDL()
    )
    assert result["ok"] is False
    assert result["message"] == "Searching YouTube Music failed: down"
    result = run(job, ytmusic=FakeYTMusic([]), youtube_dl=FakeYoutubeDL())
    assert result["message"] == "Nothing on YouTube Music or YouTube looks like this track."


@pytest.mark.parametrize(
    ("error", "plain"),
    [
        ("ERROR: [youtube] x: Video unavailable", "isn't available"),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "HTTP 403"),
        ("HTTP Error 429: Too Many Requests", "HTTP 429"),
        ("ERROR: Requested format is not available", "no audio"),
        ("ERROR: Something new", "Something new"),
    ],
)
def test_explain(error, plain):
    assert plain in explain(error)


async def test_the_downloader_turns_answers_into_results(tmp_path):
    jobs = []

    async def runner(job):
        jobs.append(job)
        if job["title"] == "Missing":
            return {"ok": False, "message": "Nothing found.", "tried": [{"reason": "x"}]}
        return {
            **{"ok": True, "path": "/s/a.opus", "format": "opus", "bitrate": 128},
            **{"duration": 200.0, "origin": "https://y/v", "cover_url": None, "album": "A"},
        }

    downloader = YouTubeDownloader(config_dir=tmp_path, pause=0, runner=runner)
    result = await downloader.download(SourceTrack("A", "Song", duration=200), tmp_path)
    assert (result.path, result.format, result.downloader) == (Path("/s/a.opus"), "opus", "youtube")
    assert jobs[0]["staging_dir"] == str(tmp_path) and jobs[0]["threshold"] == 85
    with pytest.raises(DownloaderError, match="Nothing found") as error:
        await downloader.download(SourceTrack("A", "Missing"), tmp_path)
    assert error.value.tried == [{"reason": "x"}]


async def test_a_hanging_download_is_given_up(tmp_path):
    async def slow(job):
        import asyncio

        await asyncio.sleep(10)

    downloader = YouTubeDownloader(config_dir=tmp_path, pause=0, runner=slow, timeout=0.01)
    with pytest.raises(DownloaderError, match="took longer"):
        await downloader.download(SourceTrack("A", "B"), tmp_path)


async def test_the_worker_process_speaks_json(monkeypatch):
    """A real child process, with a module that only echoes JSON (no network)."""
    monkeypatch.setattr(youtube, "JOB_MODULE", "json.tool")
    assert await run_in_fresh_process({"ok": True, "n": 1}) == {"ok": True, "n": 1}
    monkeypatch.setattr(youtube, "JOB_MODULE", "app.no_such_module")
    with pytest.raises(WorkerFailed, match="No module named"):
        await run_in_fresh_process({})


def test_the_job_answers_on_stdout_only(monkeypatch):
    def fake_run(job):
        print("a library printing something")  # must not spoil the answer
        return {"ok": True, "seen": job["title"]}

    monkeypatch.setattr(youtube_job, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"title": "T"})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    youtube_job.main()
    assert json.loads(out.getvalue()) == {"ok": True, "seen": "T"}
