"""Is this the same track? Normalising names and scoring how similar two tracks are.

Services spell the same song differently: "Beyoncé" / "Beyonce", "Song (feat. X)" / "Song",
"Song - Remastered 2011" / "Song". ``normalise`` removes those differences.

Titles are split into the **song** and its **version** (``title_parts``):

- *Hard* versions make a different track and are never mixed up: "Extended Mix", "Imanbek
  Remix", "Live", "Acoustic", "Instrumental", "Taylor's Version" … For a DJ, "Strobe (Extended
  Mix)" and "Strobe (Radio Edit)" are two tracks.
- *Soft* notes don't change the track and are dropped: "Remastered 2011", "Radio Edit", "Original
  Mix", "Album Version", "feat. X", "(Official Audio)" …

Two tracks match when their songs are alike, their hard versions are the same, the main artist
agrees and their lengths are no more than ``DURATION_TOLERANCE`` seconds apart (when both are
known). An exact MusicBrainz ID or ISRC beats all of that (``match_song``).
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from rapidfuzz import fuzz

DURATION_TOLERANCE = 10  # seconds two lengths of the same track may differ

# "(feat. X)", "[ft. X]", " feat. X", " featuring X" up to the end or the next bracket. Never at
# the very start, so a title like "Feat of Strength" stays.
_FEATURING = re.compile(
    r"(?:\s+|\s*[(\[])\s*\b(?:feat|ft|featuring)\b\.?\s[^)\]]*[)\]]?", re.IGNORECASE
)
# "(Remastered 2011)", "[2011 Remaster]", " - Remastered", " - 2009 Remastered Version"
_REMASTER = re.compile(
    r"\s*(?:[(\[][^)\]]*\bremaster(?:ed)?\b[^)\]]*[)\]]|-\s+[^-]*\bremaster(?:ed)?\b.*$)",
    re.IGNORECASE,
)
# Bracketed parts that are not at the start: "Song (Extended Mix) [Club Edit]".
_BRACKETS = re.compile(r"(?<=\S)\s*[(\[]([^)\]]*)[)\]]")
# " - Extended Mix", " – Live": a part after a dash with spaces around it.
_DASH = re.compile(r"\s+[-–—]\s+")
# Between artists: "A feat. B", "A & B", "A, B", "A x B", "A vs. B", "A with B", "A and B".
_ARTIST_SEPARATORS = re.compile(
    r"\s*(?:,|;|/|&|\+|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\bwith\b|\bx\b|\bvs\b\.?|\band\b)\s*",
    re.IGNORECASE,
)

# Words that make a version a different track (checked as whole words).
HARD_VERSION_WORDS = {
    "remix", "rmx", "mix", "edit", "version", "live", "acoustic", "instrumental", "dub",
    "rework", "bootleg", "vip", "extended", "club", "acapella", "cappella", "demo", "unplugged",
    "reprise", "cover", "karaoke", "sped", "slowed", "nightcore", "orchestral", "flip", "remake",
}  # fmt: skip
# Notes that don't change the track (the whole part, digits ignored).
SOFT_VERSIONS = {
    "radio edit", "radio version", "radio mix", "single version", "single edit", "album version",
    "original mix", "original version", "original", "edit", "explicit", "explicit version",
    "clean", "clean version", "mono", "stereo", "mono version", "stereo version", "bonus track",
    "deluxe", "deluxe edition", "remaster", "remastered", "remastered version", "official audio",
    "official video", "official music video", "music video", "official lyric video",
    "lyric video", "lyrics", "lyric", "audio", "video", "visualizer", "visualiser", "hd", "hq",
    "4k", "official", "mv",
}  # fmt: skip
# Parts that start like this are an artist credit or a note, never a version.
_SOFT_STARTS = ("feat ", "ft ", "featuring ", "with ", "from ", "taken from ", "prod ")


def strip_accents(text: str) -> str:
    """'Beyoncé' -> 'Beyonce', 'Sigur Rós' -> 'Sigur Ros'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _plain(text: str) -> str:
    """Lowercase, no accents, "&" as "and", only letters, digits and single spaces."""
    text = strip_accents(text).casefold().replace("&", " and ")
    text = "".join(c if c.isalnum() else " " for c in text)
    return " ".join(text.split())


def normalise(text: str | None) -> str:
    """Lowercase, without accents, featured artists, remaster notes and punctuation.

    ``normalise("Get Lucky (feat. Pharrell Williams) - Remastered 2013")`` -> ``"get lucky"``
    """
    text = text or ""
    text = _REMASTER.sub("", text)
    text = _FEATURING.sub(" ", text)
    return _plain(text)


def similarity(a: str | None, b: str | None) -> float:
    """0–100: how alike two names are after normalising (word order doesn't matter)."""
    left, right = normalise(a), normalise(b)
    if not left or not right:
        return 0.0
    return fuzz.token_sort_ratio(left, right)


# --- Titles: the song and its version -------------------------------------------------------


@dataclass(frozen=True)
class TitleParts:
    song: str  # normalised, without versions and notes: "strobe"
    versions: tuple[str, ...]  # normalised hard versions: ("extended mix",)


def _kind_of_part(part: str) -> str:
    """ "soft", "hard" or "title" (a bracket that belongs to the name, like "(Part 2)")."""
    plain = _plain(part)
    if not plain or plain.startswith(_SOFT_STARTS) or plain in ("feat", "ft"):
        return "soft"
    if "remaster" in plain:
        return "soft"
    without_digits = " ".join(w for w in plain.split() if not w.isdigit())
    if without_digits in SOFT_VERSIONS or not without_digits:
        return "soft"
    if set(plain.split()) & HARD_VERSION_WORDS:
        return "hard"
    return "title"


def title_parts(title: str | None) -> TitleParts:
    """Split a title into the song and its hard versions.

    ``title_parts("Strobe (Extended Mix)")`` -> ``TitleParts("strobe", ("extended mix",))``
    ``title_parts("Song - Remastered 2011")`` -> ``TitleParts("song", ())``
    """
    text = _FEATURING.sub(" ", title or "")
    versions: list[str] = []

    def bracket(match: re.Match) -> str:
        kind = _kind_of_part(match.group(1))
        if kind == "hard":
            versions.append(_plain(match.group(1)))
        return f" {match.group(1)} " if kind == "title" else " "

    text = _BRACKETS.sub(bracket, text)
    head, *tails = _DASH.split(text)
    kept = [head]
    for tail in tails:
        kind = _kind_of_part(tail)
        if kind == "hard":
            versions.append(_plain(tail))
        elif kind == "title":
            kept.append(tail)
    song = _plain(" ".join(kept)) or _plain(title or "")
    return TitleParts(song, tuple(sorted(set(versions))))


def same_versions(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True when two titles name the same versions (both none, or alike names)."""
    if len(a) != len(b):
        return False
    return all(any(fuzz.ratio(x, y) >= 85 for y in b) for x in a) and all(
        any(fuzz.ratio(y, x) >= 85 for x in a) for y in b
    )


def title_score(wanted: str | None, found: str | None) -> float:
    """0–100: how alike two titles are. 0 when they name different versions."""
    left, right = title_parts(wanted), title_parts(found)
    if not left.song or not right.song or not same_versions(left.versions, right.versions):
        return 0.0
    return fuzz.token_sort_ratio(left.song, right.song)


# --- Artists ----------------------------------------------------------------------------------


def split_artists(text: str | None) -> list[str]:
    """The single artists of a credit, normalised, main artist first.
    ``split_artists("Daft Punk feat. Pharrell Williams & Nile Rodgers")`` ->
    ``["daft punk", "pharrell williams", "nile rodgers"]``"""
    plain = strip_accents(text or "").casefold()
    names = [_plain(name) for name in _ARTIST_SEPARATORS.split(plain)]
    return [name for name in names if name]


def main_artist(text: str | None) -> str:
    names = split_artists(text)
    return names[0] if names else ""


def artist_score(wanted: str | None, found: str | list[str] | None) -> float:
    """0–100: how well the wanted (main) artist matches a found artist credit (or several,
    like the OpenSubsonic ``artists`` list). "Daft Punk" matches "Daft Punk feat. Pharrell"."""
    credits = found if isinstance(found, list) else [found or ""]
    wanted_main = main_artist(wanted)
    if not wanted_main:
        return 0.0
    best = 0.0
    for credit in credits:
        names = split_artists(credit)
        best = max([best, *(fuzz.ratio(wanted_main, name) for name in names)])
        best = max(best, fuzz.token_sort_ratio(_plain(wanted or ""), _plain(credit or "")))
    return best


# --- Whole tracks -----------------------------------------------------------------------------


def durations_agree(wanted: float | None, found: float | None) -> bool:
    """True when both lengths are within DURATION_TOLERANCE, or one of them is unknown."""
    if not wanted or not found:
        return True
    return abs(wanted - found) <= DURATION_TOLERANCE


def track_score(
    wanted_artist: str,
    wanted_title: str,
    found_artist: str | list[str],
    found_title: str,
    wanted_duration: float | None = None,
    found_duration: float | None = None,
) -> float:
    """0–100: how likely a found song is the wanted track. Titles count more than artists
    (services list featured artists differently); different versions score low; lengths more
    than 10 seconds apart cost points."""
    title = title_score(wanted_title, found_title)
    artist = artist_score(wanted_artist, found_artist)
    score = 0.6 * title + 0.4 * artist
    if wanted_duration and found_duration:
        off_by = abs(wanted_duration - found_duration)
        if off_by > DURATION_TOLERANCE:
            score -= min(30.0, (off_by - DURATION_TOLERANCE) / 2)
    return max(0.0, round(score, 1))


def is_match(score: float, threshold: int) -> bool:
    """True when a score reaches the MATCH_THRESHOLD setting."""
    return score >= threshold


def track_key(artist: str | None, title: str | None) -> str:
    """A key for "the same track" when a service gives no ISRC or MBID:
    main artist, song and hard versions, normalised. ``"daft punk|get lucky|"``"""
    parts = title_parts(title)
    return f"{main_artist(artist)}|{parts.song}|{'+'.join(parts.versions)}"


# --- Songs in Navidrome -----------------------------------------------------------------------


class Wanted(Protocol):
    """A track as Releasomat wants it (a SourceTrack or a Track row)."""

    artist: str
    title: str
    duration: float | None
    isrc: str | None
    mbid: str | None


@dataclass(frozen=True)
class SongMatch:
    song: dict  # the Subsonic song
    method: str  # "mbid", "isrc" or "name"
    score: float


def song_artists(song: dict) -> list[str]:
    """Every artist credit of a Subsonic song: artist, displayArtist, OpenSubsonic artists."""
    names = [song.get("artist") or "", song.get("displayArtist") or ""]
    names += [a.get("name", "") for a in song.get("artists") or [] if isinstance(a, dict)]
    return [n for n in names if n]


def song_isrcs(song: dict) -> set[str]:
    value = song.get("isrc") or []
    values = value if isinstance(value, list) else [value]
    return {str(v).strip().upper() for v in values if v}


def match_song(wanted: Wanted, songs: list[dict], threshold: int) -> SongMatch | None:
    """The song among ``songs`` (Subsonic search results) that is the wanted track, or None.

    1. the same MusicBrainz recording ID; 2. the same ISRC (and a similar title);
    3. by name: similar song and main artist, the same versions, lengths within 10 s, and a
    score of at least ``threshold``.
    """
    mbid = (wanted.mbid or "").strip().lower()
    if mbid:
        for song in songs:
            if (song.get("musicBrainzId") or "").strip().lower() == mbid:
                return SongMatch(song, "mbid", 100.0)
    isrc = (wanted.isrc or "").strip().upper()
    if isrc:
        for song in songs:
            if isrc in song_isrcs(song) and title_score(wanted.title, song.get("title")) >= 60:
                return SongMatch(song, "isrc", 100.0)
    best: SongMatch | None = None
    for song in songs:
        if not durations_agree(wanted.duration, song.get("duration")):
            continue
        score = track_score(
            wanted.artist,
            wanted.title,
            song_artists(song),
            song.get("title") or "",
            wanted.duration,
            song.get("duration"),
        )
        if is_match(score, threshold) and (best is None or score > best.score):
            best = SongMatch(song, "name", score)
    return best


def search_text(artist: str | None, title: str | None) -> str:
    """What to type into a search box: main artist and song, without versions' brackets and
    punctuation that search engines trip over. ``"daft punk get lucky"``"""
    parts = title_parts(title)
    return " ".join(p for p in (main_artist(artist), parts.song, *parts.versions) if p)


# --- YouTube results ------------------------------------------------------------------------

# Words in a found title that make it a different recording unless the wanted track names them
# too (Explo's keyword filter, but never blocking what the playlist asks for).
UNWANTED_WORDS = {
    "live", "remix", "rmx", "cover", "karaoke", "instrumental", "acoustic", "nightcore", "slowed",
    "sped", "reaction", "tutorial", "lesson", "8d", "extended", "acapella", "cappella", "reverb",
}  # fmt: skip
_CHANNEL_NOISE = re.compile(r"(?:\s*-\s*topic|\s*vevo|\s*official)+\s*$", re.IGNORECASE)


def unwanted_words(wanted_artist: str, wanted_title: str, found_title: str) -> set[str]:
    """UNWANTED_WORDS in a found title that the wanted artist and title don't contain:
    ``unwanted_words("Daft Punk", "Get Lucky", "Get Lucky (Live at the Grammys)") == {"live"}``"""
    wanted = set(_plain(f"{wanted_artist} {wanted_title}").split())
    return (set(_plain(found_title).split()) & UNWANTED_WORDS) - wanted


def channel_artist(channel: str | None) -> str:
    """The artist behind a YouTube channel name: "Daft Punk - Topic" -> "Daft Punk"."""
    return _CHANNEL_NOISE.sub("", channel or "").strip()


def video_score(
    wanted_artist: str, wanted_title: str, video_title: str, channel: str | None = None
) -> float:
    """0–100: how likely a YouTube video is the wanted track. Video titles usually look like
    "Artist - Title (Official Video)"; the channel ("Artist - Topic", "ArtistVEVO") also tells
    the artist."""
    credits = [c for c in (channel_artist(channel),) if c]
    scores = [0.0]
    head, *rest = _DASH.split(video_title or "", maxsplit=1)
    if rest:
        scores.append(track_score(wanted_artist, wanted_title, [head, *credits], rest[0]))
        scores.append(track_score(wanted_artist, wanted_title, [rest[0], *credits], head))
    wanted, found = title_parts(wanted_title), title_parts(video_title)
    if wanted.song and same_versions(wanted.versions, found.versions):
        title = fuzz.token_set_ratio(wanted.song, found.song)
        main = main_artist(wanted_artist)
        in_title = bool(main) and f" {main} " in f" {_plain(video_title)} "
        artist = 100.0 if in_title else artist_score(wanted_artist, credits) if credits else 0.0
        scores.append(round(0.6 * title + 0.4 * artist, 1))
    return max(scores)
