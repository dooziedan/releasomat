"""Error reports: what went wrong in a refresh, in plain words, plus what's needed to fix it.

A run keeps a report (``run.report``, JSON) when the refresh failed or single tracks failed
(docs/architecture.md, "Error reports"; ADR 0006). The playlist page and the Activity page show
the plain message, the details on demand, and a **Copy report** button that copies ``as_text()``
(ready to paste into a GitHub issue or a chat).

A report never contains passwords, tokens, API keys or cookies: everything goes through
``scrub()`` before it is stored, which removes the values of secret settings, Navidrome's login
token and salt, cookie values and anything that looks like a credential in a web address or
header.
"""

import re
import traceback
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from sqlmodel import Session

from app import __version__
from app import settings as user_settings
from app.models import utcnow

STEPS = {
    "read": "read the playlist",
    "match": "look for songs in Navidrome",
    "download": "download",
    "scan": "let Navidrome scan",
    "playlist": "update the Navidrome playlist",
}
EXCERPT = 500  # characters of an answer kept in a report
HIDDEN = "[hidden]"

# Credentials in addresses and headers: Subsonic's t= (token) s= (salt) p= (password), API keys,
# bearer tokens, cookies.
_PATTERNS = [
    re.compile(r"(?i)([?&;](?:t|s|p|token|salt|password|apikey|api_key|key|secret|sig)=)[^&\s\"'<>]+"),
    re.compile(r"(?i)((?:authorization|x-nd-authorization|x-api-key)\s*[:=]\s*)(?:bearer\s+)?[^\s,\"']+"),
    re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\n\"']+"),
    re.compile(r"(?i)(\"?(?:password|token|secret|api_?key|client_secret|access_token|refresh_token|subsonic(?:Token|Salt))\"?\s*[:=]\s*\"?)[^\s\",}&<>']+"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}"),
]  # fmt: skip
# Settings whose values are secret: removed wherever they appear.
SECRET_SETTINGS = [d.key for d in user_settings.DEFINITIONS if d.secret]


def cookie_values(cookies_file: Path) -> list[str]:
    """The values in a Netscape cookies.txt (the 7th column), to remove them from reports."""
    try:
        lines = cookies_file.read_text(errors="replace").splitlines()
    except OSError:
        return []
    values = []
    for line in lines:
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and len(parts[6].strip()) >= 6:
            values.append(parts[6].strip())
    return values


def known_secrets(session: Session | None = None, config_dir: Path | None = None) -> list[str]:
    """Every secret value Releasomat knows: secret settings and cookie values."""
    found: list[str] = []
    if session is not None:
        found += [user_settings.get(session, key) for key in SECRET_SETTINGS]
    if config_dir is not None:
        found += cookie_values(Path(config_dir) / "cookies.txt")
    return [s for s in found if s and len(s) >= 4]


def scrub(value, secrets: list[str] = ()):
    """``value`` (text, or dicts and lists of text) with every secret removed."""
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    text = value
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, HIDDEN)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + HIDDEN, text)
    return text


def versions() -> dict[str, str]:
    """Releasomat, yt-dlp and Deno versions as installed right now (an updated yt-dlp shows
    up here at once, even before a download uses it)."""
    found = {"releasomat": __version__}
    for name, package in (("yt_dlp", "yt-dlp"), ("deno", "deno")):
        try:
            found[name] = version(package)
        except PackageNotFoundError:
            found[name] = "not installed"
    return found


def excerpt(text: object, limit: int = EXCERPT) -> str:
    return " ".join(str(text or "").split())[:limit]


def failure(
    step: str,
    exc: BaseException,
    *,
    prefix: str | None = None,
    source: str | None = None,
    link: str | None = None,
    secrets: list[str] = (),
    now: datetime | None = None,
) -> dict:
    """The report of a refresh that failed at ``step`` because of ``exc``.

    A ``SourceError`` or ``NavidromeError`` brings its own plain message (shown after
    ``prefix``, e.g. "Apple Music: …") and ``details`` (``url``, ``http_status``, ``excerpt``,
    …); any other exception is described by its type, message and where in the code it
    happened."""
    details = dict(getattr(exc, "details", None) or {})
    known = hasattr(exc, "details")
    message = str(exc) if known else f"Unexpected error: {type(exc).__name__}: {exc}"
    if prefix and known and not message.startswith(prefix):
        message = f"{prefix}: {message}"
    report = {
        "message": message,
        "step": step,
        "step_label": STEPS.get(step, step),
        "source": source,
        "link": details.pop("url", None) or link,
        "error_type": type(exc).__name__,
        "http_status": details.pop("http_status", None),
        "excerpt": excerpt(details.pop("excerpt", "")) or None,
        "details": {k: excerpt(v) for k, v in details.items() if v not in (None, "")},
        "versions": versions(),
        "time": (now or utcnow()).isoformat(timespec="seconds"),
        "tracks": [],
    }
    if not known:
        frames = traceback.extract_tb(exc.__traceback__)[-4:]
        report["details"]["where"] = " ← ".join(
            f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames)
        )
    return scrub(report, secrets)


def for_tracks(
    failed: list[dict],
    *,
    source: str | None = None,
    link: str | None = None,
    secrets: list[str] = (),
    now: datetime | None = None,
) -> dict:
    """The report of a refresh that worked but couldn't get some tracks. ``failed``: one entry
    per track: ``position``, ``artist``, ``title``, ``message``, ``tried``."""
    count = len(failed)
    message = f"{count} track{'s' if count != 1 else ''} couldn't be downloaded."
    return _without_error(message, "download", {}, failed, source, link, secrets, now)


def stopped(
    message: str,
    step: str,
    *,
    details: dict | None = None,
    failed: list[dict] = (),
    source: str | None = None,
    link: str | None = None,
    secrets: list[str] = (),
    now: datetime | None = None,
) -> dict:
    """The report of a refresh the owner stopped (Stop button): the step it was in, what it was
    doing (``details``, e.g. the downloads that were cut off) and the tracks that had failed
    before. Not an error, but the same shape, so pages and Copy report treat it the same way."""
    details = {k: excerpt(v) for k, v in (details or {}).items() if v not in (None, "")}
    return _without_error(message, step, details, list(failed), source, link, secrets, now)


def _without_error(message, step, details, tracks, source, link, secrets, now) -> dict:
    report = {
        "message": message,
        "step": step,
        "step_label": STEPS.get(step, step),
        "source": source,
        "link": link,
        "error_type": None,
        "http_status": None,
        "excerpt": None,
        "details": details,
        "versions": versions(),
        "time": (now or utcnow()).isoformat(timespec="seconds"),
        "tracks": tracks,
    }
    return scrub(report, secrets)


def as_text(report: dict | None, playlist: str | None = None) -> str:
    """The report as plain text for the Copy report button."""
    if not report:
        return ""
    lines = ["Releasomat error report", ""]
    if playlist:
        lines.append(f"Playlist: {playlist}")
    lines.append(f"What happened: {report.get('message')}")
    lines.append(f"Step: {report.get('step_label') or report.get('step')}")
    for label, key in (
        ("Source", "source"),
        ("Link", "link"),
        ("Error type", "error_type"),
        ("HTTP status", "http_status"),
        ("Answer (excerpt)", "excerpt"),
    ):
        if report.get(key) not in (None, ""):
            lines.append(f"{label}: {report[key]}")
    for key, value in (report.get("details") or {}).items():
        lines.append(f"{key}: {value}")
    for track in report.get("tracks") or []:
        lines.append("")
        lines.append(f"Track {track.get('position')}: {track.get('artist')} – {track.get('title')}")
        lines.append(f"  Problem: {track.get('message')}")
        for attempt in track.get("tried") or []:
            lines.append(
                f"  - {attempt.get('downloader')}: {attempt.get('candidate')}: "
                f"{attempt.get('reason')}"
            )
    versions_ = report.get("versions") or {}
    lines.append("")
    lines.append(
        "Versions: Releasomat {releasomat}, yt-dlp {yt_dlp}, Deno {deno}".format(
            releasomat=versions_.get("releasomat", "?"),
            yt_dlp=versions_.get("yt_dlp", "?"),
            deno=versions_.get("deno", "?"),
        )
    )
    lines.append(f"Time: {report.get('time')}")
    return "\n".join(lines)
