"""Fixtures for the Soulseek tests.

The fake slskd (``scripts/fake_slskd.py``) stands in for the owner's slskd. Its web app is
answered **in-process** through httpx's ASGI transport: no port, no network, nothing online. It
runs 100 times faster than in development, and instead of making audio files with ffmpeg it
copies small test files (a 2-second tone, or 40 seconds of silence for a "wrong file").
"""

import shutil
from pathlib import Path

import httpx
import pytest

from app import files
from app.downloaders.slskd import SlskdDownloader
from scripts.fake_slskd import SCENARIOS, FakeSlskd, Song

AUDIO = Path(__file__).parent / "audio"
FIXTURE_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"
URL = "http://slskd.test:5030"
KEY = "test-key-5f2c9a7e41b3"  # 16+ characters, like slskd wants

# Songs are 4 seconds long, so the 2-second test tones count as the right length.
SONGS = [
    Song("Daft Punk", "Get Lucky", "Random Access Memories", 4, 2013, 8),
    Song("Beyoncé", "Halo", "I Am... Sasha Fierce", 4, 2008, 2),
    Song("Queen", "Bohemian Rhapsody", "A Night at the Opera", 4, 1975, 11),
    *(Song("Releasomat Test", word, "Scenario Songs", 4, 2026, n) for n, word in
      enumerate(SCENARIOS[1:], start=1)),
]  # fmt: skip


def copy_audio(target: Path, ext: str, seconds: float) -> None:
    """Instead of ffmpeg: a 2-second tone in the format asked for (MP3 at 320 kbit/s), or 40
    seconds of silence when the file should be much longer than the song."""
    if seconds > 15:
        source = AUDIO / "silence-40s.flac"
    elif ext == "mp3":
        source = AUDIO / "tone-320.mp3"
    else:
        source = FIXTURE_AUDIO / f"tone.{ext}"
        if not source.exists():
            source = FIXTURE_AUDIO / "tone.flac"
    shutil.copyfile(source, target)


@pytest.fixture
def staging(tmp_path) -> Path:
    return files.new_staging_dir(tmp_path / "config")


@pytest.fixture
async def make_fake(tmp_path):
    """``make_fake(scenario="stall", …)``: a fake slskd with its own downloads folder."""
    made: list[FakeSlskd] = []

    def make(**options) -> FakeSlskd:
        downloads = tmp_path / "slskd"
        downloads.mkdir(exist_ok=True)
        options.setdefault("speed", 100)
        options.setdefault("make_audio", copy_audio)
        fake = FakeSlskd(SONGS, downloads, KEY, **options)
        made.append(fake)
        return fake

    yield make
    for fake in made:
        await fake.close()


@pytest.fixture
async def fake(make_fake) -> FakeSlskd:
    return make_fake()


@pytest.fixture
def api_key() -> str:
    return KEY


@pytest.fixture
def make_downloader():
    """``make_downloader(fake, formats=[…], …)``: a downloader that talks to ``fake``
    in-process, with short waits."""

    def make(fake: FakeSlskd, *, key: str = KEY, **options) -> SlskdDownloader:
        settings = {
            "downloads_dir": fake.downloads_dir,
            "transport": httpx.ASGITransport(app=fake.app),
            "poll": 0.01,
            "search_time": 2.0,
            "stall_limit": 0.3,
            "max_time": 5.0,
            "file_wait": 1.0,
        }
        return SlskdDownloader(URL, key, **{**settings, **options})

    return make
