"""One YouTube download, run in a fresh worker process (``app/downloaders/youtube.py``).

Why a separate process: yt-dlp breaks every few weeks when YouTube changes something, and an
updated yt-dlp only takes effect in a Python process that imports it anew. Every download starts
a new process, so an update applies to the next download without restarting Releasomat
(ADR 0007).

What happens here, for one track:

1. **Search** YouTube Music for songs (ytmusicapi, ``filter="songs"``): the official audio of a
   release. Each result is checked: artist and title (versions kept apart, see
   ``app/matching.py``), length within 10 seconds, no unwanted words like "live" or "cover".
   Only if none of them works: a YouTube video search (``ytsearch5:``), checked the same way.
2. **Download** the best audio stream with yt-dlp as it is, without re-encoding (YouTube's Opus
   or M4A; ffmpeg only changes the container) into the staging folder.
3. **Check** the file: it must be readable audio, and its length must fit the wanted track.
   Otherwise it is discarded and the next candidate is tried.

Everything goes in and out as plain dicts (JSON between the processes, see ``main``). Nothing here
writes outside the staging folder; ``app/files.py`` moves the file into MUSIC_DIR later.
"""

import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app import files, matching
from app.tagging import TaggingError, audio_info

DOWNLOADER = "youtube"
MAX_DOWNLOADS = 3  # candidates downloaded per track before giving up
MAX_REJECTED_SHOWN = 5  # rejected search results listed in the error report


@dataclass
class Candidate:
    url: str
    title: str
    artist: str
    duration: float | None
    album: str | None
    thumbnail: str | None
    where: str  # "YouTube Music" or "YouTube"
    score: float = 0.0

    @property
    def label(self) -> str:
        length = f" ({clock(self.duration)})" if self.duration else ""
        return f"{self.where}: {self.artist} – {self.title}{length} {self.url}"


def clock(seconds: float | None) -> str:
    """225 -> "3:45"."""
    if not seconds:
        return "?"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d}"


class _Log:
    """Collects yt-dlp's warnings and errors instead of printing them."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.lines.append(message)

    def error(self, message: str) -> None:
        self.lines.append(message)


def explain(error: str) -> str:
    """A yt-dlp error message in plain words (the original is kept in the report)."""
    text = error.lower()
    if "sign in to confirm" in text or "not a bot" in text:
        return (
            "YouTube asks to sign in to confirm this isn't a bot. Try again later, or add a "
            "cookies.txt (see the docs)."
        )
    if "confirm your age" in text or "age-restricted" in text:
        return "The video is age-restricted (only works with cookies of an adult account)."
    if "http error 403" in text:
        return "YouTube refused the download (HTTP 403). A newer yt-dlp usually fixes this."
    if "http error 429" in text or "too many requests" in text:
        return "YouTube limits downloads right now (HTTP 429). Try again later."
    if "requested format is not available" in text:
        return "YouTube offered no audio for this video."
    if "video unavailable" in text or "not available" in text or "private video" in text:
        return "The video isn't available (removed, private or blocked in this country)."
    if "ffmpeg" in text or "ffprobe" in text:
        return "ffmpeg couldn't process the download."
    first = error.strip().splitlines()[0] if error.strip() else "Unknown error"
    return first.removeprefix("ERROR: ")[:300]


def _check(job: dict, title: str, artists: list[str], duration: float | None) -> str | None:
    """Why a search result is not the wanted track, or None if it may be."""
    if not matching.durations_agree(job.get("duration"), duration):
        return f"different length: {clock(duration)} instead of {clock(job.get('duration'))}"
    unwanted = matching.unwanted_words(job["artist"], job["title"], title)
    if unwanted:
        return f"another version ({', '.join(sorted(unwanted))})"
    wanted, found = matching.title_parts(job["title"]), matching.title_parts(title)
    if not matching.same_versions(wanted.versions, found.versions):
        return f"another version ({', '.join(found.versions) or 'without the version wanted'})"
    return None


def _thumbnail(thumbnails: list[dict] | None) -> str | None:
    """The biggest thumbnail of a YouTube Music result, asked for at 544 × 544."""
    if not thumbnails:
        return None
    url = thumbnails[-1].get("url") or ""
    return re.sub(r"=w\d+-h\d+", "=w544-h544", url) or None


def search_youtube_music(job: dict, ytmusic=None) -> tuple[list[Candidate], list[dict]]:
    """Songs on YouTube Music that may be the track (best first), and the rejected ones."""
    if ytmusic is None:
        from ytmusicapi import YTMusic

        ytmusic = YTMusic()
    query = matching.search_text(job["artist"], job["title"]) or f"{job['artist']} {job['title']}"
    results = ytmusic.search(query, filter="songs", limit=10)
    found, rejected = [], []
    for result in results or []:
        video_id = result.get("videoId")
        if not video_id:
            continue
        artists = [a.get("name", "") for a in result.get("artists") or [] if a.get("name")]
        candidate = Candidate(
            url=f"https://music.youtube.com/watch?v={video_id}",
            title=result.get("title") or "",
            artist=", ".join(artists),
            duration=result.get("duration_seconds"),
            album=(result.get("album") or {}).get("name"),
            thumbnail=_thumbnail(result.get("thumbnails")),
            where="YouTube Music",
        )
        if result.get("isAvailable") is False:
            rejected.append(_rejected(candidate, "not available"))
            continue
        reason = _check(job, candidate.title, artists, candidate.duration)
        candidate.score = matching.track_score(
            job["artist"], job["title"], artists, candidate.title, job.get("duration"),
            candidate.duration,
        )  # fmt: skip
        if reason is None and not matching.is_match(candidate.score, job["threshold"]):
            reason = f"artist or title don't match (score {candidate.score:.0f})"
        if reason:
            rejected.append(_rejected(candidate, reason))
        else:
            found.append(candidate)
    found.sort(key=lambda c: -c.score)  # stable: equal scores keep YouTube Music's order
    return found, rejected


def search_youtube(job: dict, youtube_dl=None) -> tuple[list[Candidate], list[dict]]:
    """Videos on YouTube (``ytsearch5:``) that may be the track, and the rejected ones."""
    query = f"ytsearch5:{job['artist']} - {job['title']}"
    options = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}
    options["logger"] = _Log()
    with _youtube_dl(options, youtube_dl) as ydl:
        info = ydl.extract_info(query, download=False) or {}
    found, rejected = [], []
    for entry in info.get("entries") or []:
        video_id = entry.get("id")
        if not video_id:
            continue
        channel = entry.get("channel") or entry.get("uploader") or ""
        candidate = Candidate(
            url=f"https://www.youtube.com/watch?v={video_id}",
            title=entry.get("title") or "",
            artist=channel,
            duration=entry.get("duration"),
            album=None,
            thumbnail=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            where="YouTube",
        )
        reason = _check(job, candidate.title, [channel], candidate.duration)
        candidate.score = matching.video_score(
            job["artist"], job["title"], candidate.title, channel
        )
        if reason is None and not matching.is_match(candidate.score, job["threshold"]):
            reason = f"artist or title don't match (score {candidate.score:.0f})"
        if reason:
            rejected.append(_rejected(candidate, reason))
        else:
            found.append(candidate)
    found.sort(key=lambda c: -c.score)
    return found, rejected


def _rejected(candidate: Candidate, reason: str) -> dict:
    return {"downloader": DOWNLOADER, "candidate": candidate.label, "reason": reason}


def _youtube_dl(options: dict, factory=None):
    if factory is not None:
        return factory(options)
    import yt_dlp

    return yt_dlp.YoutubeDL(options)


def download_options(job: dict, staging: Path, log: _Log) -> dict:
    """yt-dlp's options: best audio as it is (Opus or M4A, no re-encoding), into staging."""
    options = {
        "format": "bestaudio/best",
        "paths": {"home": str(staging), "temp": str(staging)},
        "outtmpl": "%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "logger": log,
        # "best": keep the codec, only put it into its usual container (webm/opus -> .opus).
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}],
        "cachedir": str(Path(job["config_dir"]) / "cache" / "yt-dlp"),
        "socket_timeout": 30,
        "retries": 3,
        "overwrites": False,
    }
    cookies = Path(job["config_dir"]) / "cookies.txt"
    if cookies.is_file():
        # A copy: yt-dlp writes cookies back when it is done, the owner's file stays as it is.
        copy = staging / "cookies.txt"
        shutil.copyfile(cookies, copy)
        options["cookiefile"] = str(copy)
    return options


def download_candidate(job: dict, candidate: Candidate, youtube_dl=None) -> Path:
    """Download one candidate into the staging folder; returns the file. Raises RuntimeError
    with yt-dlp's message when the download fails."""
    staging = Path(job["staging_dir"])
    log = _Log()
    with _youtube_dl(download_options(job, staging, log), youtube_dl) as ydl:
        info = ydl.extract_info(candidate.url, download=True) or {}
    downloads = info.get("requested_downloads") or [info]
    filepath = downloads[0].get("filepath") or downloads[0].get("_filename")
    if not filepath or not Path(filepath).is_file():
        raise RuntimeError("; ".join(log.lines) or "yt-dlp finished without a file")
    return Path(filepath)


def run(job: dict, *, ytmusic=None, youtube_dl=None) -> dict:
    """Find, download and check one track. Returns a dict: ``ok``, and either the file's
    ``path``, ``format``, ``bitrate``, ``duration``, ``origin``, ``cover_url``, ``album``, or a
    plain ``message``; in both cases ``tried`` (what was rejected and why)."""
    tried: list[dict] = []
    result = {"ok": False, "tried": tried, "yt_dlp_version": yt_dlp_version()}
    search_errors: list[str] = []
    download_errors: list[str] = []
    searches = (
        ("YouTube Music", search_youtube_music, ytmusic),
        ("YouTube", search_youtube, youtube_dl),
    )
    for where, search, client in searches:
        if len(download_errors) >= MAX_DOWNLOADS:
            break
        try:
            candidates, rejected = search(job, client)
        except Exception as exc:
            message = f"Searching {where} failed: {explain(str(exc))}"
            search_errors.append(message)
            tried.append({"downloader": DOWNLOADER, "candidate": where, "reason": message})
            continue
        tried.extend(rejected[:MAX_REJECTED_SHOWN])
        for candidate in candidates[: MAX_DOWNLOADS - len(download_errors)]:
            if download_errors:
                time.sleep(job.get("pause", 0))  # a short break before the next try
            outcome = _try(job, candidate, youtube_dl)
            if isinstance(outcome, dict):
                result.update(outcome, ok=True)
                return result
            download_errors.append(outcome)
            tried.append(_rejected(candidate, outcome))
    if download_errors:
        count = len(download_errors)
        result["message"] = f"{count} download(s) tried, none worked: {download_errors[-1]}"
    elif search_errors:
        result["message"] = search_errors[0]
    else:
        result["message"] = "Nothing on YouTube Music or YouTube looks like this track."
    return result


def _try(job: dict, candidate: Candidate, youtube_dl) -> dict | str:
    """Download and check one candidate: a dict for the pipeline, or why it didn't work."""
    staging = Path(job["staging_dir"])
    try:
        path = download_candidate(job, candidate, youtube_dl)
    except Exception as exc:
        return explain(str(exc))
    try:
        info = audio_info(path)
    except TaggingError as exc:
        files.discard_staged(staging, path)
        return f"the download isn't readable audio ({exc})"
    expected = job.get("duration") or candidate.duration
    if not matching.durations_agree(expected, info.duration):
        files.discard_staged(staging, path)
        return f"the file is {clock(info.duration)} long instead of {clock(expected)}"
    return {
        "path": str(path),
        "format": info.format,
        "bitrate": info.bitrate,
        "duration": info.duration,
        "origin": candidate.url,
        "cover_url": candidate.thumbnail,
        "album": candidate.album,
    }


def yt_dlp_version() -> str | None:
    try:
        from yt_dlp.version import __version__
    except ImportError:
        return None
    return __version__


def main() -> None:
    """``python -m app.downloaders.youtube_job``: the job as JSON on stdin, the answer as JSON on
    stdout. Anything libraries print goes to stderr, so it can't spoil the answer."""
    job = json.load(sys.stdin)
    answer = sys.stdout
    sys.stdout = sys.stderr
    try:
        result = run(job)
    except Exception as exc:  # the parent turns this into the track's error
        result = {"ok": False, "message": f"{type(exc).__name__}: {exc}", "tried": []}
    answer.write(json.dumps(result))
    answer.flush()


if __name__ == "__main__":
    main()
