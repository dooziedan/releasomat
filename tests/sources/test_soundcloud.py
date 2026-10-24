"""Reading SoundCloud sets with yt-dlp (saved answers, never online)."""

import pytest
from yt_dlp.utils import DownloadError

from app.sources import ytdlp
from app.sources.base import SourceError, detect_source
from app.sources.soundcloud import OPTIONS, SoundCloud

REF = detect_source("https://soundcloud.com/soundcloud-the-peak/sets/on-the-up-new-edm-hits")


async def test_reads_a_set(saved, make, monkeypatch):
    calls = []

    def extract(url, **options):
        calls.append((url, options))
        return saved.json("soundcloud_set.json"), []

    monkeypatch.setattr(ytdlp, "extract", extract)
    snapshot = await make(SoundCloud).fetch(REF)

    # Track details, but no audio formats (they would cost several requests per track).
    assert calls == [(REF.url, OPTIONS)]
    assert OPTIONS["extractor_args"] == {"soundcloud": {"formats": ["none"]}}
    assert snapshot.title == "New EDM Hits: On The Up"
    assert snapshot.cover_url.startswith("https://i1.sndcdn.com/")
    assert [(t.artist, t.title) for t in snapshot.tracks] == [
        ("Mochakk", "Neckwear (feat. Channel Tres)"),
        ("Sara Landry, Alt8, Saint Monet", "Feel 4 U (feat. Saint Monet)"),
        ("Alesso, Kaitlin Aragon", "Fight For Love"),
        ("MK", "Obsession (feat. Ty Dolla $ign)"),
    ]
    first = snapshot.tracks[0]
    assert (first.duration, first.source_id, first.album) == (147.738, "2415921885", None)
    assert first.cover_url.startswith("https://i1.sndcdn.com/artworks-")
    assert snapshot.warnings == ["Skipped 1 tracks SoundCloud didn't give details for."]


async def test_artist_from_the_title_or_the_uploader(saved, make, monkeypatch):
    data = saved.json("soundcloud_set.json")
    data["entries"][0].update(artists=None, artist=None, title="Mochakk - Neckwear (Extended Mix)")
    data["entries"][1].update(artists=None, artist=None, title="Feel 4 U", uploader="Sara Landry")
    data["entries"][2].update(album_type="album", album="Fight For Love EP")
    monkeypatch.setattr(ytdlp, "extract", lambda url, **options: (data, []))
    tracks = (await make(SoundCloud).fetch(REF)).tracks
    assert (tracks[0].artist, tracks[0].title) == ("Mochakk", "Neckwear (Extended Mix)")
    assert (tracks[1].artist, tracks[1].title) == ("Sara Landry", "Feel 4 U")
    assert tracks[2].album == "Fight For Love EP"  # a set that is an album


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        (["ERROR: [soundcloud:set] x/sets/y: Unable to download JSON metadata: HTTP Error 404: "
          "Not Found"], "can't find this playlist"),
        (["ERROR: [soundcloud:set] x/sets/y: HTTP Error 429: Too Many Requests"], "limiting"),
        ([], "yt-dlp couldn't read"),
    ],
)  # fmt: skip
async def test_the_set_itself_fails(make, monkeypatch, errors, expected):
    """With ignoreerrors, yt-dlp answers None and only logs why."""
    monkeypatch.setattr(ytdlp, "extract", lambda url, **options: (None, errors))
    with pytest.raises(SourceError, match=expected) as caught:
        await make(SoundCloud).fetch(REF)
    assert caught.value.details["url"] == REF.url and caught.value.details["ytdlp_version"]


async def test_unexpected_yt_dlp_errors(make, monkeypatch):
    def extract(url, **options):
        raise DownloadError("ERROR: Unable to extract client id")

    monkeypatch.setattr(ytdlp, "extract", extract)
    with pytest.raises(SourceError, match="Updating yt-dlp may help") as caught:
        await make(SoundCloud).fetch(REF)
    assert caught.value.details["excerpt"] == "Unable to extract client id"


async def test_no_readable_tracks(saved, make, monkeypatch):
    data = saved.json("soundcloud_set.json")
    data["entries"] = [None, None]
    errors = ["ERROR: [soundcloud] 1: This track is not available in your country"]
    monkeypatch.setattr(ytdlp, "extract", lambda url, **options: (data, errors))
    with pytest.raises(SourceError, match="has no tracks") as caught:
        await make(SoundCloud).fetch(REF)
    assert caught.value.details["errors"] == [errors[0]]
