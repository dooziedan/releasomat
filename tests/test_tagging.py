"""Tags and cover art in downloaded files (Opus, M4A, MP3, FLAC, AIFF, WAV), and their length."""

import shutil
from pathlib import Path

import pytest

from app.tagging import TaggingError, Tags, audio_info, cover_mime, read_tags, write_tags

AUDIO = Path(__file__).parent / "fixtures" / "audio"
MBID = "1a2b3c4d-1234-4abc-8def-0123456789ab"
TAGS = Tags(
    title="Get Lucky",
    artist="Daft Punk feat. Pharrell Williams",
    album="Random Access Memories",
    isrc="USQX91300108",
    mbid=MBID,
)


@pytest.mark.parametrize(
    "name", ["tone.opus", "tone.m4a", "tone.mp3", "tone.flac", "tone.aiff", "tone.wav"]
)
def test_tags_and_cover_are_written(tmp_path, name):
    path = tmp_path / name
    shutil.copy(AUDIO / name, path)
    write_tags(path, TAGS, (AUDIO / "cover.jpg").read_bytes())
    assert read_tags(path) == {
        "title": "Get Lucky",
        "artist": "Daft Punk feat. Pharrell Williams",
        "album": "Random Access Memories",
        "isrc": "USQX91300108",
        "mbid": MBID,
        "cover": "yes",
    }
    assert 1.5 < audio_info(path).duration < 2.5  # still the same audio


def test_without_cover_or_with_a_picture_players_dont_show(tmp_path):
    path = tmp_path / "tone.opus"
    shutil.copy(AUDIO / "tone.opus", path)
    write_tags(path, Tags(title="T", artist="A"), b"RIFF....WEBPVP8 not a jpeg")
    assert read_tags(path) == {"title": "T", "artist": "A"}
    assert cover_mime((AUDIO / "cover.jpg").read_bytes()) == "image/jpeg"
    assert cover_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"


def test_audio_info():
    info = audio_info(AUDIO / "tone.opus")
    assert info.format == "opus" and info.bitrate and round(info.duration) == 2


def test_broken_or_unknown_files_are_refused(tmp_path):
    with pytest.raises(TaggingError):
        audio_info(AUDIO / "broken.opus")
    other = tmp_path / "tone.wma"
    other.write_bytes(b"x")
    with pytest.raises(TaggingError, match="can't tag .wma"):
        write_tags(other, TAGS)
