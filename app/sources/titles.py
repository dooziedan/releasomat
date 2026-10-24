"""Artist and title out of video and upload titles, for YouTube and SoundCloud.

Videos rarely say who made a song in a field of its own. Their titles usually do:
"Daft Punk - Get Lucky (Official Video)". Sometimes the other way round, with the channel's name
on the right: "BELLAKEO (Video Oficial) - Peso Pluma, Anitta" on Peso Pluma's channel. YouTube's
automatic "Artist - Topic" channels have the plain song title.
"""

import re

# "(Official Video)", "[Official Music Video]", "(Video Oficial)", "(Lyric Video)", "[HD]"…
# Mix names like "(Extended Mix)" or "(Live)" stay: they make a different track.
_NOISE = re.compile(
    r"\s*[(\[][^()\[\]]*\b(?:official|video|audio|lyrics?|visuali[sz]er|oficial|officiel|hd|hq"
    r"|4k|mv|m/v|clip|letra)\b[^()\[\]]*[)\]]",
    re.IGNORECASE,
)
_DASH = re.compile(r"\s+[-–—]\s+")
# The part after the dash is a mix name, not a title: "Get Lucky - Radio Edit".
_MIX = re.compile(
    r"^[^-–—]{0,40}\b(?:mix|remix|edit|version|dub|rework|bootleg|vip|remaster(?:ed)?)$",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    """'Get Lucky (Official Video) [HD]' -> 'Get Lucky'."""
    return " ".join(_NOISE.sub("", title).split()) or title.strip()


def artist_from_channel(channel: str | None) -> str | None:
    """'Daft Punk - Topic' -> 'Daft Punk', 'DaftPunkVEVO' -> 'DaftPunk'."""
    if not channel:
        return None
    name = re.sub(r"\s+-\s+Topic$", "", channel.strip())
    name = re.sub(r"(?:\s*VEVO|\s+Official|\s+Oficial)$", "", name, flags=re.IGNORECASE)
    return name.strip() or None


def split_artist_title(title: str, channel: str | None) -> tuple[str | None, str]:
    """(artist, title) from a video or upload title and the channel or uploader's name.

    'Daft Punk - Get Lucky (Official Video)'      -> ('Daft Punk', 'Get Lucky')
    'BELLAKEO - Peso Pluma, Anitta' on Peso Pluma -> ('Peso Pluma, Anitta', 'BELLAKEO')
    'Get Lucky' on 'Daft Punk - Topic'            -> ('Daft Punk', 'Get Lucky')
    'Get Lucky - Radio Edit' on Daft Punk         -> ('Daft Punk', 'Get Lucky (Radio Edit)')
    The artist is None when neither the title nor the channel names one.
    """
    clean = clean_title(title)
    artist = artist_from_channel(channel)
    parts = _DASH.split(clean, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        left, right = parts
        if _MIX.match(right) and artist:
            return artist, f"{left} ({right})"
        if artist and _names(right, artist) and not _names(left, artist):
            return right, left
        return left, right
    return artist, clean


def _names(text: str, artist: str) -> bool:
    """True when the text starts with the artist's name (ignoring case, spaces, punctuation)."""
    simple = "".join(c for c in text.casefold() if c.isalnum())
    name = "".join(c for c in artist.casefold() if c.isalnum())
    return bool(name) and simple.startswith(name)
