"""User settings: connections to other services and defaults (architecture.md, "Configuration").

The owner edits them on the Settings page; they are saved in the ``setting`` table. An env var
with the same name (``NAVIDROME_URL``…) wins over the saved value and locks the field: the page
shows it read-only as "set by the container". So a value comes from, in this order:

    env var  >  saved in the database  >  built-in default

An env var that is set but empty does not count: Unraid passes optional template fields that the
owner left blank as empty variables, and those must not lock the field.

Secrets (passwords, API keys, tokens) are never logged and never sent back to the browser in full:
pages and the API only show ``mask(value)``.

A "list" setting (``DOWNLOAD_SOURCES``, ``SLSKD_FORMATS``) is a comma-separated list of names in
order, e.g. ``slskd,youtube``. ``none`` means that every entry is switched off (an empty value
means "back to the default", like for every other setting).

The group "Tagwerk" is optional: Releasomat works in full without it (ADR 0009). Its key only
switches on the integration API that Tagwerk calls.
"""

import os
from dataclasses import dataclass, field
from typing import Literal

from sqlmodel import Session

from app.models import Setting

Kind = Literal["text", "url", "secret", "int", "bool", "list"]

NONE = "none"  # a "list" setting with every entry switched off


@dataclass(frozen=True)
class Definition:
    key: str  # also the env var's name
    label: str
    group: str
    kind: Kind = "text"
    default: str = ""
    help: str = ""
    minimum: int | None = None  # for kind "int"
    maximum: int | None = None
    choices: tuple[str, ...] = ()  # for kind "list": the allowed names, in their default order
    allow_none: bool = False  # for kind "list": may every entry be switched off?
    min_length: int | None = None  # for kinds "text" and "secret": at least this many characters
    # A key another program sends in an HTTP header: letters, digits and punctuation, no spaces.
    header_safe: bool = False

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


GROUPS = {
    "navidrome": "Navidrome",
    "sources": "Download sources",
    "slskd": "Soulseek (slskd)",
    "listenbrainz": "ListenBrainz",
    "tidal": "Tidal",
    "downloads": "Downloads and matching",
    "playlists": "Playlists in Navidrome",
    "tagwerk": "Tagwerk",
}
# A line under a group's heading.
GROUP_NOTES = {
    "tagwerk": "Optional. Only needed if you use Tagwerk to manage your library: it lets Tagwerk "
    "adopt Releasomat's downloads into your library. Releasomat works the same without it.",
}

DEFINITIONS = [
    Definition(
        "NAVIDROME_URL", "Address", "navidrome", "url",
        help="For example http://192.168.1.10:4533, or http://navidrome:4533 on a shared Docker "
        "network. Not localhost: inside the container that is Releasomat itself.",
    ),
    Definition("NAVIDROME_USER", "User", "navidrome", help="A Navidrome user with admin rights."),
    Definition(
        "NAVIDROME_PASSWORD", "Password", "navidrome", "secret",
        help="Sent to Navidrome only as a salted token.",
    ),
    Definition(
        "NAVIDROME_SUBFOLDER", "Releasomat's folder in Navidrome", "navidrome",
        default="releasomat",
        help="The name of Releasomat's music folder as Navidrome sees it inside its library.",
    ),
    Definition(
        "DOWNLOAD_SOURCES", "Order for all playlists", "sources", "list", "slskd,youtube",
        help="Each missing track is tried with these sources from top to bottom, until one "
        "delivers a checked file. The same order for every playlist; a source that isn't "
        "connected is skipped.",
        choices=("slskd", "youtube"), allow_none=True,
    ),
    Definition(
        "SLSKD_URL", "Address", "slskd", "url",
        help="slskd's web address, for example http://192.168.1.10:5030. Not localhost.",
    ),
    Definition(
        "SLSKD_API_KEY", "API key", "slskd", "secret", help="An API key from slskd's settings.",
    ),
    Definition(
        "SLSKD_FORMATS", "Formats, best first", "slskd", "list", "flac,aiff,wav,mp3",
        help="Releasomat takes the best of these that Soulseek offers. A track with none of them "
        "goes to the next download source.",
        choices=("flac", "aiff", "wav", "mp3", "m4a", "ogg", "opus"),
    ),
    Definition(
        "SLSKD_MIN_BITRATE", "Minimum bitrate for lossy files", "slskd", "int", "320",
        help="In kbit/s, for MP3, M4A, Ogg and Opus. Lossless files (FLAC, AIFF, WAV) always pass.",
        minimum=128, maximum=320,
    ),
    Definition("LISTENBRAINZ_USER", "User name", "listenbrainz"),
    Definition(
        "LISTENBRAINZ_TOKEN", "User token", "listenbrainz", "secret",
        help="From listenbrainz.org → Settings. Needed for private playlists.",
    ),
    Definition("TIDAL_CLIENT_ID", "Client ID", "tidal"),
    Definition(
        "TIDAL_CLIENT_SECRET", "Client secret", "tidal", "secret",
        help="From a free app on developer.tidal.com. Only needed for Tidal playlists.",
    ),
    Definition(
        "DOWNLOAD_CONCURRENCY", "Downloads at the same time", "downloads", "int", "2",
        help="Shared by all playlists.", minimum=1, maximum=8,
    ),
    Definition(
        "MATCH_THRESHOLD", "Match threshold", "downloads", "int", "85",
        help="How similar (0–100) a found song must be to count as the same track.",
        minimum=50, maximum=100,
    ),
    Definition(
        "PATH_TEMPLATE", "File names", "downloads", default="{artist}/{artist} - {title}.{ext}",
        help="Inside Releasomat's music folder. Placeholders: {artist}, {title}, {album}, {ext}.",
    ),
    Definition(
        "SNAPSHOT_AUTO_DELETE", "Delete old snapshot playlists automatically", "playlists", "bool",
        "false",
        help="Off: old snapshot playlists stay in Navidrome until you delete them. On: Releasomat "
        "keeps the newest N per playlist (set on each playlist) and deletes older ones it "
        "created. Music files are never deleted.",
    ),
    Definition(
        "INTEGRATION_API_KEY", "Integration key", "tagwerk", "secret",
        help="Empty (the default): the integration is off. With a key, Tagwerk may call "
        "Releasomat's integration API; enter the same key in Tagwerk. At least 16 characters; "
        "Generate makes a good one.",
        min_length=16, header_safe=True,
    ),
]  # fmt: skip

TRUE_WORDS = ("true", "1", "yes", "on")
FALSE_WORDS = ("false", "0", "no", "off")

BY_KEY = {d.key: d for d in DEFINITIONS}

# How the names in "list" settings are shown.
CHOICE_LABELS = {
    "slskd": "Soulseek",
    "youtube": "YouTube",
    "flac": "FLAC",
    "aiff": "AIFF",
    "wav": "WAV",
    "mp3": "MP3",
    "m4a": "M4A (AAC)",
    "ogg": "Ogg Vorbis",
    "opus": "Opus",
}
LOSSLESS = ("flac", "aiff", "wav")


class SettingLocked(Exception):
    """The setting is set by an env var, so it can't be changed in the UI."""


class InvalidSetting(ValueError):
    """A value that doesn't fit the setting (shown next to the field)."""


def env_value(key: str) -> str | None:
    """The env var's value, or None when it is not set or empty."""
    value = os.environ.get(key, "")
    return value if value.strip() else None


def is_locked(key: str) -> bool:
    """True when an env var sets this setting (the field is read-only in the UI)."""
    return env_value(key) is not None


def get(session: Session, key: str) -> str:
    """The setting's value: env var, else saved value, else default."""
    definition = BY_KEY[key]
    if (value := env_value(key)) is not None:
        return value
    row = session.get(Setting, key)
    return row.value if row is not None else definition.default


def get_int(session: Session, key: str) -> int:
    """An "int" setting as a number; the default if the value is not a valid number."""
    try:
        return _check_int(BY_KEY[key], get(session, key))
    except InvalidSetting:
        return int(BY_KEY[key].default)


def get_bool(session: Session, key: str) -> bool:
    """A "bool" setting (a switch on the Settings page; env var "true" or "false")."""
    return get(session, key).strip().lower() in TRUE_WORDS


def get_list(session: Session, key: str) -> list[str]:
    """A "list" setting as names in order, [] when every entry is switched off ("none"). The
    default if the value doesn't fit (e.g. an env var with an unknown name)."""
    definition = BY_KEY[key]
    try:
        value = clean(definition, get(session, key))
    except InvalidSetting:
        value = definition.default
    return names_of(value or definition.default)


def names_of(value: str) -> list[str]:
    """ "slskd,youtube" → ["slskd", "youtube"]; "none" → []."""
    return [] if value == NONE else [name for name in value.split(",") if name]


def slskd_set_up(session: Session) -> bool:
    """Whether slskd's address and API key are both set (Soulseek is skipped otherwise)."""
    return bool(get(session, "SLSKD_URL") and get(session, "SLSKD_API_KEY"))


def describe_sources(names: list[str], slskd_ready: bool = True) -> str:
    """The download order in words: "Soulseek, then YouTube"; "" when none is switched on."""
    words = []
    for name in names:
        word = CHOICE_LABELS.get(name, name)
        if name == "slskd" and not slskd_ready:
            word += " (skipped: not connected)"
        words.append(word)
    return ", then ".join(words)


def source_of(session: Session, key: str) -> Literal["container", "saved", "default"]:
    if is_locked(key):
        return "container"
    return "saved" if session.get(Setting, key) is not None else "default"


def save(session: Session, key: str, value: str) -> None:
    """Save a value from the UI or API. An empty value goes back to the default.

    Raises SettingLocked when an env var sets this setting, InvalidSetting for a bad value.
    """
    definition = BY_KEY[key]
    if is_locked(key):
        raise SettingLocked(f"{key} is set by the container")
    value = clean(definition, value)
    row = session.get(Setting, key)
    if value == "":
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.commit()


def clean(definition: Definition, value: str) -> str:
    """The value as it is saved; raises InvalidSetting when it doesn't fit."""
    value = (value or "").strip()
    if value == "":
        return ""
    if definition.kind == "int":
        return str(_check_int(definition, value))
    if definition.kind == "bool":
        if value.lower() in TRUE_WORDS:
            return "true"
        if value.lower() in FALSE_WORDS:
            return "false"
        raise InvalidSetting("Use true or false")
    if definition.kind == "url":
        if not value.startswith(("http://", "https://")):
            raise InvalidSetting("Start with http:// or https://")
        return value.rstrip("/")
    if definition.kind == "list":
        return _check_list(definition, value)
    if definition.min_length and len(value) < definition.min_length:
        raise InvalidSetting(f"At least {definition.min_length} characters")
    if definition.header_safe and not all(33 <= ord(c) <= 126 for c in value):
        raise InvalidSetting("Only letters, digits and punctuation, no spaces")
    return value


def _check_list(definition: Definition, value: str) -> str:
    """ "slskd, YouTube" → "slskd,youtube": known names, each only once; "none" if allowed."""
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    if names in ([], [NONE]):
        if definition.allow_none:
            return NONE
        raise InvalidSetting("Switch on at least one")
    unknown = [name for name in names if name not in definition.choices]
    if unknown:
        raise InvalidSetting(f"Unknown: {unknown[0]}. Use {', '.join(definition.choices)}")
    if len(set(names)) != len(names):
        raise InvalidSetting("Each one only once")
    return ",".join(names)


def _check_int(definition: Definition, value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise InvalidSetting("Enter a whole number") from None
    low, high = definition.minimum, definition.maximum
    if (low is not None and number < low) or (high is not None and number > high):
        raise InvalidSetting(f"Between {low} and {high}")
    return number


def mask(value: str) -> str:
    """A secret as it may be shown: dots, plus its last 2 characters if it is long enough."""
    if not value:
        return ""
    return "••••••••" + (value[-2:] if len(value) >= 12 else "")


@dataclass
class View:
    """One setting as the Settings page and the API show it (secrets masked)."""

    key: str
    label: str
    group: str
    kind: Kind
    help: str
    default: str
    value: str  # masked for secrets
    is_set: bool
    locked: bool
    source: str  # "container", "saved" or "default"
    choices: list[str] = field(default_factory=list)  # for kind "list": the allowed names


def view(session: Session, key: str) -> View:
    definition = BY_KEY[key]
    value = get(session, key)
    return View(
        key=key,
        label=definition.label,
        group=definition.group,
        kind=definition.kind,
        help=definition.help,
        default=definition.default,
        value=mask(value) if definition.secret else value,
        is_set=bool(value),
        locked=is_locked(key),
        source=source_of(session, key),
        choices=list(definition.choices),
    )


def all_views(session: Session) -> list[View]:
    return [view(session, d.key) for d in DEFINITIONS]
