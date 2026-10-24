"""Make a small demo music library for the dev Navidrome.

Creates about twenty quiet audio files (a sine tone or noise, no real music) with the tags and the
real length of well-known songs, so playlists from Apple Music, Deezer or ListenBrainz find some of
them "already in Navidrome" (Releasomat only links a song whose length is within 10 seconds of the
playlist's track). The files are mono at a low sample rate, about 20 MB together. Some entries are
deliberately awkward, to test matching:

- "feat." in the title (Uptown Funk) and in the artist (Get Lucky)
- a "- Remastered 2011" suffix in the title (Bohemian Rhapsody), and the plain title where streaming
  services add one (Don't Stop Me Now, Dreams)
- accents and umlauts (Beyoncé, Motörhead, Sigur Rós, Grönemeyer)
- "and" where streaming services write "&" (Simon and Garfunkel), "&" and a comma in a name
  (Earth, Wind & Fire)
- the same title by two artists (Halo, Hurt)
- a remix without its original (Roses (Imanbek Remix))

Files land in dev/library/Demo/<Album artist>/<Album>/NN Title.ext as mp3, flac, m4a and opus.
Only needs Python's standard library and ffmpeg (with ffprobe). Safe to run again: existing files
are kept, except the 5–8 second files older versions of this script made, which are replaced.

    python3 scripts/make_demo_library.py               # into dev/library/Demo
    python3 scripts/make_demo_library.py other/folder  # somewhere else
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = ROOT / "dev" / "library" / "Demo"

# One entry per track: (artist, title, album, track number, year, genre, format, length in
# seconds), plus the album artist where it differs from the artist.
# fmt: off
TRACKS = [
    ("Queen", "Bohemian Rhapsody - Remastered 2011", "A Night at the Opera", 11, 1975, "Rock",
     "mp3", 355),
    ("Queen", "Don't Stop Me Now", "Jazz", 12, 1978, "Rock", "flac", 209),
    ("Daft Punk feat. Pharrell Williams & Nile Rodgers", "Get Lucky", "Random Access Memories", 8,
     2013, "Electronic", "m4a", 369, "Daft Punk"),
    ("Mark Ronson", "Uptown Funk (feat. Bruno Mars)", "Uptown Special", 4, 2015, "Funk", "opus",
     270),
    ("Beyoncé", "Halo", "I Am... Sasha Fierce", 2, 2008, "R&B", "mp3", 261),
    ("Depeche Mode", "Halo", "Violator", 4, 1990, "Synth-pop", "flac", 270),
    ("Motörhead", "Ace of Spades", "Ace of Spades", 1, 1980, "Heavy Metal", "mp3", 169),
    ("Sigur Rós", "Hoppípolla", "Takk...", 2, 2005, "Post-Rock", "opus", 268),
    ("Simon and Garfunkel", "The Sound of Silence", "Sounds of Silence", 1, 1966, "Folk", "m4a",
     186),
    ("Earth, Wind & Fire", "September", "The Best of Earth, Wind & Fire, Vol. 1", 7, 1978, "Funk",
     "mp3", 215),
    ("SAINt JHN", "Roses (Imanbek Remix)", "Roses (Imanbek Remix)", 1, 2019, "Dance", "m4a", 176),
    ("The Weeknd", "Blinding Lights", "After Hours", 9, 2020, "Synth-pop", "mp3", 200),
    ("Dua Lipa", "Levitating", "Future Nostalgia", 5, 2020, "Pop", "opus", 203),
    ("Fleetwood Mac", "Dreams", "Rumours", 2, 1977, "Rock", "flac", 257),
    ("Nirvana", "Smells Like Teen Spirit", "Nevermind", 1, 1991, "Grunge", "flac", 301),
    ("Billie Eilish", "bad guy", "WHEN WE ALL FALL ASLEEP, WHERE DO WE GO?", 2, 2019, "Pop", "m4a",
     194),
    ("Rammstein", "Du hast", "Sehnsucht", 5, 1997, "Industrial Metal", "mp3", 234),
    ("Herbert Grönemeyer", "Männer", "4630 Bochum", 2, 1984, "Pop", "flac", 238),
    ("Harry Styles", "As It Was", "Harry's House", 4, 2022, "Pop", "opus", 167),
    ("a-ha", "Take On Me", "Hunting High and Low", 1, 1985, "Synth-pop", "mp3", 225),
    ("Johnny Cash", "Hurt", "American IV: The Man Comes Around", 2, 2002, "Country", "m4a", 218),
    ("Nine Inch Nails", "Hurt", "The Downward Spiral", 14, 1994, "Industrial", "opus", 373),
]
# fmt: on

# ffmpeg audio settings per format (small files: mono, 8 or 16 kHz, low bitrate)
SAMPLE_RATE = {"opus": 16000}  # Opus needs 8, 12, 16, 24 or 48 kHz; the others get 8 kHz
CODECS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "16k", "-id3v2_version", "3"],
    "flac": ["-c:a", "flac", "-compression_level", "8"],
    "m4a": ["-c:a", "aac", "-b:a", "16k"],
    "opus": ["-c:a", "libopus", "-b:a", "12k"],
}


def safe_name(text: str) -> str:
    """Replace characters that are not allowed in file names on Windows/SMB shares."""
    return re.sub(r'[<>:"/\\|?*]', "_", text).strip(" .")


def length_of(path: Path) -> float:
    """The length of an audio file in seconds (0 if ffprobe can't read it)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def make_track(target: Path, number: int, track: tuple) -> bool:
    """Create one file. Returns True if it was created, False if it already existed."""
    artist, title, album, track_no, year, genre, ext, duration = track[:8]
    album_artist = track[8] if len(track) > 8 else artist
    filename = f"{track_no:02d} {safe_name(title)}.{ext}"
    path = target / safe_name(album_artist) / safe_name(album) / filename
    # Files of older versions of this script were only 5-8 seconds long: those are made again.
    if path.exists() and length_of(path) > duration - 10:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)

    rate = SAMPLE_RATE.get(ext, 8000)
    if number % 3 == 0:
        source = f"anoisesrc=color=pink:amplitude=0.02:sample_rate={rate}:duration={duration}"
    else:
        source = f"sine=frequency={220 + 40 * number}:sample_rate={rate}:duration={duration}"
    tags = {
        "artist": artist,
        "album_artist": album_artist,
        "title": title,
        "album": album,
        "track": str(track_no),
        "date": str(year),
        "genre": genre,
    }
    # Write to a temporary name first, so an interrupted run never leaves a half-written file
    # that the next run would skip.
    partial = path.with_name(f".partial-{path.name}")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-f", "lavfi", "-i", source, "-ac", "1", "-af", "volume=0.2", *CODECS[ext]]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-f", {"m4a": "ipod", "opus": "ogg"}.get(ext, ext), str(partial)]
    try:
        subprocess.run(cmd, check=True)
        partial.replace(path)  # also replaces an old 5-8 second file
    finally:
        partial.unlink(missing_ok=True)
    return True


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_TARGET
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg is needed but was not found. Install it first (`sudo pacman -S ffmpeg`).")
        return 1
    created = sum(make_track(target, number, track) for number, track in enumerate(TRACKS))
    print(f"Demo library: {len(TRACKS)} tracks in {target} ({created} new)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
