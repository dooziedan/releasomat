"""Reading and writing tags of downloaded files with mutagen.

Only files Releasomat downloaded itself are tagged, right after the download and while they are
still in the staging folder (before ``app/files.py`` moves them into MUSIC_DIR). Songs already in
the library are never touched.

Tags written: title, artist, album, ISRC, the MusicBrainz recording ID (Navidrome reads it as
``musicbrainz_trackid`` and can then find the song by that ID), a comment saying where the file
came from, and the cover. Tag names differ per format (the same ones MusicBrainz Picard uses):

- Opus / Ogg / FLAC: MUSICBRAINZ_TRACKID, ISRC, METADATA_BLOCK_PICTURE
- M4A: ----:com.apple.iTunes:MusicBrainz Track Id, ----:com.apple.iTunes:ISRC, covr
- MP3 (ID3): UFID http://musicbrainz.org, TSRC, APIC
- AIFF / WAV: the same ID3 frames, inside the file's own ID3 chunk
"""

import base64
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TIT2, TPE1, TSRC, UFID
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

MUSICBRAINZ_OWNER = "http://musicbrainz.org"


class TaggingError(Exception):
    """The file couldn't be read or tagged (unknown format, broken file)."""


@dataclass(frozen=True)
class Tags:
    title: str
    artist: str
    album: str | None = None
    isrc: str | None = None
    mbid: str | None = None  # MusicBrainz recording ID
    comment: str | None = None


@dataclass(frozen=True)
class AudioInfo:
    duration: float  # seconds
    bitrate: int | None  # kbit/s
    format: str  # "opus", "m4a", "mp3", "flac", "ogg"


def audio_info(path: Path) -> AudioInfo:
    """Length, bitrate and format of an audio file. Raises TaggingError for anything that isn't
    a readable audio file (the download is then treated as wrong)."""
    try:
        audio = mutagen.File(path)
    except Exception as exc:  # mutagen raises many kinds of errors for broken files
        raise TaggingError(f"Can't read {path.name}: {exc}") from None
    if audio is None or not getattr(audio, "info", None):
        raise TaggingError(f"{path.name} is not an audio file Releasomat can read.")
    length = float(getattr(audio.info, "length", 0) or 0)
    bitrate = getattr(audio.info, "bitrate", 0) or 0
    return AudioInfo(
        duration=round(length, 1),
        bitrate=round(bitrate / 1000) if bitrate else None,
        format=path.suffix.lower().lstrip(".") or "unknown",
    )


def cover_mime(data: bytes) -> str | None:
    """ "image/jpeg" or "image/png" for the picture formats every player shows, else None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def write_tags(path: Path, tags: Tags, cover: bytes | None = None) -> None:
    """Write the tags (and the cover, if it is a JPEG or PNG) into a downloaded file."""
    suffix = path.suffix.lower()
    mime = cover_mime(cover) if cover else None
    try:
        if suffix in (".opus", ".ogg", ".oga", ".flac"):
            _vorbis(path, suffix, tags, cover if mime else None, mime)
        elif suffix in (".m4a", ".mp4", ".aac"):
            _mp4(path, tags, cover if mime else None, mime)
        elif suffix == ".mp3":
            _id3(path, tags, cover if mime else None, mime)
        elif suffix in (".aiff", ".aif", ".wav"):
            _chunk_id3(path, suffix, tags, cover if mime else None, mime)
        else:
            raise TaggingError(f"Releasomat can't tag {suffix} files yet.")
    except TaggingError:
        raise
    except Exception as exc:
        raise TaggingError(f"Tagging {path.name} failed: {exc}") from None


def _vorbis(path: Path, suffix: str, tags: Tags, cover: bytes | None, mime: str | None) -> None:
    if suffix == ".flac":
        audio = FLAC(path)
    elif suffix == ".opus":
        audio = OggOpus(path)
    else:
        audio = mutagen.File(path)
        if not isinstance(audio, (OggOpus, OggVorbis)):
            raise TaggingError(f"{path.name} is not an Ogg file Releasomat can tag.")
    values = {
        "TITLE": tags.title,
        "ARTIST": tags.artist,
        "ALBUM": tags.album,
        "ISRC": tags.isrc,
        "MUSICBRAINZ_TRACKID": tags.mbid,
        "COMMENT": tags.comment,
    }
    for key, value in values.items():
        if value:
            audio[key] = [value]
    if cover and mime:
        picture = Picture()
        picture.type = 3  # front cover
        picture.mime = mime
        picture.data = cover
        if isinstance(audio, FLAC):
            audio.clear_pictures()
            audio.add_picture(picture)
        else:
            encoded = base64.b64encode(picture.write()).decode("ascii")
            audio["METADATA_BLOCK_PICTURE"] = [encoded]
    audio.save()


def _mp4(path: Path, tags: Tags, cover: bytes | None, mime: str | None) -> None:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    simple = {"\xa9nam": tags.title, "\xa9ART": tags.artist, "\xa9alb": tags.album}
    simple["\xa9cmt"] = tags.comment
    for key, value in simple.items():
        if value:
            audio.tags[key] = [value]
    free = {"ISRC": tags.isrc, "MusicBrainz Track Id": tags.mbid}
    for name, value in free.items():
        if value:
            audio.tags[f"----:com.apple.iTunes:{name}"] = [MP4FreeForm(value.encode("utf-8"))]
    if cover and mime:
        kind = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(cover, imageformat=kind)]
    audio.save()


def _id3(path: Path, tags: Tags, cover: bytes | None, mime: str | None) -> None:
    try:
        id3 = ID3(path)
    except mutagen.id3.ID3NoHeaderError:
        id3 = ID3()
    _add_id3_frames(id3, tags, cover, mime)
    id3.save(path)


def _chunk_id3(path: Path, suffix: str, tags: Tags, cover: bytes | None, mime: str | None) -> None:
    """AIFF and WAV keep their ID3 tags in a chunk of the file, so mutagen saves them through the
    file object, not as a plain ID3 header."""
    audio = WAVE(path) if suffix == ".wav" else AIFF(path)
    if audio.tags is None:
        audio.add_tags()
    _add_id3_frames(audio.tags, tags, cover, mime)
    audio.save()


def _add_id3_frames(id3: ID3, tags: Tags, cover: bytes | None, mime: str | None) -> None:
    id3.add(TIT2(encoding=3, text=tags.title))
    id3.add(TPE1(encoding=3, text=tags.artist))
    if tags.album:
        id3.add(TALB(encoding=3, text=tags.album))
    if tags.isrc:
        id3.add(TSRC(encoding=3, text=tags.isrc))
    if tags.mbid:
        id3.add(UFID(owner=MUSICBRAINZ_OWNER, data=tags.mbid.encode("ascii", "ignore")))
    if tags.comment:
        id3.add(COMM(encoding=3, lang="eng", desc="", text=tags.comment))
    if cover and mime:
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))


def read_tags(path: Path) -> dict[str, str]:
    """The main tags of a file as plain text (for tests and checks): title, artist, album, isrc,
    mbid, and "cover" = "yes" when a picture is embedded."""
    audio = mutagen.File(path)
    if audio is None:
        return {}
    result: dict[str, str] = {}
    if isinstance(audio, MP4):
        tags = audio.tags or {}
        for key, name in (("\xa9nam", "title"), ("\xa9ART", "artist"), ("\xa9alb", "album")):
            if key in tags:
                result[name] = str(tags[key][0])
        for name, key in (("isrc", "ISRC"), ("mbid", "MusicBrainz Track Id")):
            value = tags.get(f"----:com.apple.iTunes:{key}")
            if value:
                result[name] = bytes(value[0]).decode("utf-8")
        if tags.get("covr"):
            result["cover"] = "yes"
        return result
    if isinstance(audio.tags, ID3):
        id3 = audio.tags
        for frame, name in (("TIT2", "title"), ("TPE1", "artist"), ("TALB", "album")):
            if frame in id3:
                result[name] = str(id3[frame].text[0])
        if "TSRC" in id3:
            result["isrc"] = str(id3["TSRC"].text[0])
        ufid = id3.get(f"UFID:{MUSICBRAINZ_OWNER}")
        if ufid:
            result["mbid"] = ufid.data.decode("ascii")
        if id3.getall("APIC"):
            result["cover"] = "yes"
        return result
    tags = audio.tags or {}
    for key, name in (
        ("title", "title"),
        ("artist", "artist"),
        ("album", "album"),
        ("isrc", "isrc"),
        ("musicbrainz_trackid", "mbid"),
    ):
        if key in tags:
            result[name] = str(tags[key][0])
    if "metadata_block_picture" in tags or getattr(audio, "pictures", None):
        result["cover"] = "yes"
    return result
