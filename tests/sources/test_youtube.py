"""Reading YouTube (yt-dlp) and YouTube Music (ytmusicapi) playlists (saved answers, never
online: the library calls are replaced)."""

import pytest
import requests
from ytmusicapi.exceptions import YTMusicServerError

from app.sources import youtube, ytdlp
from app.sources.base import SourceError, detect_source
from app.sources.youtube import YouTube, YouTubeMusic
from app.sources.ytdlp import extract as real_extract  # before the fixtures replace it

YTM = detect_source("https://music.youtube.com/playlist?list=PL4fGSI1pDJn6puJdseH2Rt9sMvt9E2M4i")
YT = detect_source("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI")


# --- YouTube Music ---------------------------------------------------------------------------


async def test_reads_a_youtube_music_playlist(saved, make, monkeypatch):
    asked = []

    def load(playlist_id):
        asked.append(playlist_id)
        return saved.json("youtube_music_playlist.json")

    monkeypatch.setattr(youtube, "load_ytmusic_playlist", load)
    snapshot = await make(YouTubeMusic).fetch(YTM)

    assert asked == ["PL4fGSI1pDJn6puJdseH2Rt9sMvt9E2M4i"]
    assert snapshot.title == "Top 100 Songs Global"
    assert snapshot.description == "This week's ranking of the most popular songs on YouTube."
    assert snapshot.cover_url.startswith("https://")
    assert len(snapshot.tracks) == 4
    first = snapshot.tracks[0]
    assert (first.artist, first.title, first.duration, first.source_id) == (
        "Shakira, Burna Boy",
        "Dai Dai",
        241.0,
        "fcnDmrtj6Sk",
    )
    assert first.cover_url.startswith("https://i.ytimg.com/vi/fcnDmrtj6Sk/")


async def test_youtube_music_track_with_album_and_odd_entries(saved, make, monkeypatch):
    data = saved.json("youtube_music_playlist.json")
    data["tracks"][0]["album"] = {"name": "Dai Dai (Single)", "id": "MPREb_x"}
    data["tracks"][1]["artists"] = []
    data["tracks"][2]["duration_seconds"] = None
    monkeypatch.setattr(youtube, "load_ytmusic_playlist", lambda playlist_id: data)
    snapshot = await make(YouTubeMusic).fetch(YTM)
    assert snapshot.tracks[0].album == "Dai Dai (Single)"
    assert len(snapshot.tracks) == 3 and snapshot.tracks[1].duration is None
    assert snapshot.warnings == ["Skipped 1 entries without artist or title."]


@pytest.mark.parametrize(
    ("error", "message"),
    [
        # What ytmusicapi raises for a playlist that doesn't exist or is private.
        (KeyError("Unable to find 'contents' using path ['contents', …]"), "can't find this"),
        (YTMusicServerError("Server returned HTTP 429: Too Many Requests.\n"), "limiting requests"),
        (YTMusicServerError("Server returned HTTP 500: Internal Server Error.\n"), "a problem"),
        (requests.ConnectionError("Name or service not known"), "couldn't reach YouTube Music"),
        (requests.ReadTimeout("read timed out"), "didn't answer in time"),
        (RuntimeError("something new"), "ytmusicapi couldn't read"),
    ],
)
async def test_youtube_music_errors(make, monkeypatch, error, message):
    def load(playlist_id):
        raise error

    monkeypatch.setattr(youtube, "load_ytmusic_playlist", load)
    with pytest.raises(SourceError, match=message) as caught:
        await make(YouTubeMusic).fetch(YTM)
    details = caught.value.details
    assert details["error_type"] == type(error).__name__
    assert details["ytmusicapi_version"] and details["step"] == "read source"
    assert details["url"] == YTM.url


async def test_youtube_music_changed_or_empty(saved, make, monkeypatch):
    monkeypatch.setattr(youtube, "load_ytmusic_playlist", lambda playlist_id: {"title": "x"})
    with pytest.raises(SourceError, match="doesn't understand") as caught:
        await make(YouTubeMusic).fetch(YTM)
    assert caught.value.details["found"] == ["title"]

    data = saved.json("youtube_music_playlist.json")
    data["tracks"] = []
    monkeypatch.setattr(youtube, "load_ytmusic_playlist", lambda playlist_id: data)
    with pytest.raises(SourceError, match="has no tracks"):
        await make(YouTubeMusic).fetch(YTM)


# --- YouTube ---------------------------------------------------------------------------------


async def test_reads_a_youtube_playlist(saved, make, monkeypatch):
    calls = []

    def extract(url, **options):
        calls.append((url, options))
        return saved.json("youtube_playlist.json"), []

    monkeypatch.setattr(ytdlp, "extract", extract)
    snapshot = await make(YouTube).fetch(YT)

    assert calls == [(YT.url, {"extract_flat": "in_playlist"})]  # a quick, flat list
    assert snapshot.title == "Popular Music Videos"
    assert snapshot.cover_url.startswith("https://")
    # The artist comes from the video title or the channel; video noise is gone.
    assert [(t.artist, t.title) for t in snapshot.tracks] == [
        ("Peso Pluma, Anitta", "BELLAKEO"),
        ("Arcángel", "FN8"),
        ("Becky G", "POR EL CONTRARIO ft. Ángela Aguilar, Leonardo Aguilar"),
        ("Real Boston Richey", "The Type ft. YTB Fatt"),
    ]
    assert snapshot.tracks[0].duration == 235.0 and snapshot.tracks[0].source_id == "fOT0BUpITw8"
    assert snapshot.warnings == ["Skipped 1 private, deleted or unnamed videos."]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: [youtube:tab] PLx: The playlist does not exist.", "can't find this playlist"),
        ("ERROR: [youtube:tab] PLx: This playlist is private", "can't find this playlist"),
        ("ERROR: Unable to download API page: HTTP Error 429: Too Many Requests", "limiting"),
        ("ERROR: Unable to download API page: HTTP Error 503: Service Unavailable", "a problem"),
        ("ERROR: Sign in to confirm you're not a bot", "asks Releasomat to sign in"),
        ("ERROR: Unable to download webpage: <urlopen error timed out>", "couldn't reach"),
        ("ERROR: something new", "Updating yt-dlp may help"),
    ],
)
async def test_youtube_errors(make, monkeypatch, message, expected):
    from yt_dlp.utils import DownloadError

    def extract(url, **options):
        raise DownloadError(message)

    monkeypatch.setattr(ytdlp, "extract", extract)
    with pytest.raises(SourceError, match=expected) as caught:
        await make(YouTube).fetch(YT)
    details = caught.value.details
    assert details["error_type"] == "DownloadError" and details["ytdlp_version"]
    assert details["excerpt"] == message.removeprefix("ERROR: ")


async def test_youtube_changed_or_empty(saved, make, monkeypatch):
    monkeypatch.setattr(ytdlp, "extract", lambda url, **options: ({"title": "x"}, []))
    with pytest.raises(SourceError, match="yt-dlp couldn't read"):
        await make(YouTube).fetch(YT)

    data = saved.json("youtube_playlist.json")
    data["entries"] = data["entries"][-1:]  # only the private video
    monkeypatch.setattr(ytdlp, "extract", lambda url, **options: (data, []))
    with pytest.raises(SourceError, match="has no tracks"):
        await make(YouTube).fetch(YT)


# --- The yt-dlp wrapper ----------------------------------------------------------------------


def test_extract_passes_options_and_collects_errors(monkeypatch):
    seen = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            seen.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            seen["url"], seen["download"] = url, download
            seen["logger"].error("ERROR: one track failed")
            return {"title": "x"}

        def sanitize_info(self, info):
            return dict(info, sanitized=True)

    monkeypatch.setattr("app.sources.ytdlp.yt_dlp.YoutubeDL", FakeYoutubeDL)
    info, errors = real_extract("https://example.org/list", extract_flat="in_playlist")
    assert info == {"title": "x", "sanitized": True}
    assert errors == ["ERROR: one track failed"]
    assert seen["download"] is False and seen["skip_download"] is True
    assert seen["extract_flat"] == "in_playlist" and seen["socket_timeout"] == 15
