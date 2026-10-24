"""Helpers for ``fetch()``: web requests that turn every failure into a ``SourceError``.

Every SourceError carries the details for the run's error report (docs/architecture.md, "Error
reports"): the step ("read source"), the link, the HTTP status, a short excerpt of the answer (at
most 300 characters) and the error type. Details never contain passwords, tokens, API keys or
cookies: ``source_error`` takes out the secrets a source passes as ``hide=…``, plus anything that
looks like a token ("access_token": "…", "Bearer …").
"""

import asyncio
import html
import json
import re
from collections.abc import Iterable, Sequence

import httpx

from app.sources.base import SourceError, SourceTrack

STEP = "read source"
EXCERPT_LENGTH = 300
HIDDEN = "•••"

# Things that look like secrets, whatever the source: "access_token": "abc…", token=abc…,
# "Authorization: Bearer abc…". Short values are left alone, so "token": null stays readable.
_SECRET_PATTERNS = (
    re.compile(
        r"""(["']?\b(?:access_token|refresh_token|id_token|token|client_secret|secret|password"""
        r"""|api_?key|authorization|cookie)\b["']?\s*[:=]\s*["']?)[^"'&\s,;}]{8,}""",
        re.IGNORECASE,
    ),
    re.compile(r"\b(Bearer|Token|Basic)\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
)


def scrub(text: str, hide: Iterable[str | None] = ()) -> str:
    """The text with the given secrets and anything that looks like a token replaced by •••."""
    for secret in hide:
        if secret and len(secret) >= 4:
            text = text.replace(secret, HIDDEN)
    text = _SECRET_PATTERNS[0].sub(lambda m: m.group(1) + HIDDEN, text)
    return _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)} {HIDDEN}", text)


def excerpt(text: str | bytes | None, hide: Iterable[str | None] = ()) -> str:
    """At most 300 characters of an answer, on one line, without secrets."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = scrub(" ".join((text or "").split()), hide)
    return text if len(text) <= EXCERPT_LENGTH else text[: EXCERPT_LENGTH - 1] + "…"


def source_error(
    message: str, *, hide: Iterable[str | None] = (), **details: object
) -> SourceError:
    """A SourceError for the error report: step "read source" unless given, empty details left
    out, secrets taken out of every text, the excerpt cut to 300 characters."""
    hide = tuple(hide)
    details.setdefault("step", STEP)
    clean: dict[str, object] = {}
    for key, value in details.items():
        if value is None or value == "" or value == []:
            continue
        if key == "excerpt":
            value = excerpt(value, hide)  # type: ignore[arg-type]
        elif isinstance(value, str):
            value = scrub(value, hide)
        elif isinstance(value, list | tuple):
            value = [scrub(v, hide) if isinstance(v, str) else v for v in value]
        clean[key] = value
    return SourceError(message, **clean)


async def request(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    errors: dict[int, str] | None = None,
    hide: Iterable[str | None] = (),
    retry_delays: Sequence[float] = (),
    unavailable: str | None = None,
    **kwargs,
) -> httpx.Response:
    """Send one request and return the answer if it is a success (2xx).

    Everything else raises a SourceError with a plain-language message:
    - ``errors`` maps HTTP statuses to messages (e.g. ``{404: "…can't find this playlist…"}``);
    - 429: the service limits requests; 5xx, timeouts, network errors: the service has a problem.
    ``retry_delays`` (seconds) tries again after 5xx answers, timeouts, network errors and short
    rate limits, e.g. ``(5, 15)`` for three tries within a minute. ``unavailable`` replaces the
    message when the service still has a problem after that.
    """
    hide = tuple(hide)
    not_found = f"{service} can't find this playlist. It may have been deleted, or it isn't public."
    errors = {404: not_found, **(errors or {})}
    tries = len(retry_delays) + 1
    for attempt in range(tries):
        last = attempt == tries - 1
        try:
            response = await http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            if not last:
                await asyncio.sleep(retry_delays[attempt])
                continue
            message = (
                unavailable or f"{service} didn't answer in time. The next refresh tries again."
            )
            raise source_error(
                message, url=url, error_type=type(exc).__name__, tries=tries, hide=hide
            ) from exc
        except httpx.HTTPError as exc:
            if not last:
                await asyncio.sleep(retry_delays[attempt])
                continue
            message = unavailable or f"Releasomat couldn't reach {service}. Is the internet down?"
            raise source_error(
                message,
                url=url,
                error_type=type(exc).__name__,
                excerpt=str(exc),
                tries=tries,
                hide=hide,
            ) from exc

        status = response.status_code
        if response.is_success:
            return response
        report = {
            "url": url,
            "http_status": status,
            "excerpt": response.text,
            "error_type": "HTTPStatusError",
            "hide": hide,
        }
        if status in errors:
            raise source_error(errors[status], **report)
        if status == 429:
            wait = _retry_after(response)
            if not last and wait is not None and wait <= 10:
                await asyncio.sleep(wait)
                continue
            raise source_error(
                f"{service} is limiting requests right now (too many requests). The next "
                "refresh tries again.",
                retry_after=wait,
                **report,
            )
        if status >= 500:
            if not last:
                await asyncio.sleep(retry_delays[attempt])
                continue
            message = unavailable or (
                f"{service} has a problem right now (HTTP {status}). The next refresh tries again."
            )
            raise source_error(message, tries=tries, **report)
        raise source_error(f"{service} refused the request (HTTP {status}).", **report)
    raise AssertionError("unreachable")  # the loop always returns or raises


async def get_json(http: httpx.AsyncClient, url: str, *, service: str, **kwargs) -> object:
    """GET a JSON answer (see ``request`` for the arguments and errors)."""
    response = await request(http, "GET", url, service=service, **kwargs)
    return parse_json(response, service, kwargs.get("hide", ()))


def parse_json(response: httpx.Response, service: str, hide: Iterable[str | None] = ()) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise source_error(
            f"{service} sent an answer Releasomat can't read.",
            url=str(response.request.url),
            http_status=response.status_code,
            excerpt=response.text,
            error_type=type(exc).__name__,
            hide=hide,
        ) from exc


# --- Reading JSON out of web pages -----------------------------------------------------------

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_SCRIPT_ID = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def script_ids(page: str) -> list[str]:
    """The IDs of all <script> elements of a page (what an error report lists when the one
    Releasomat looks for is missing)."""
    return [m.group(1) for s in _SCRIPT.finditer(page) if (m := _SCRIPT_ID.search(s.group(1)))]


def script_json(page: str, script_id: str) -> object | None:
    """The JSON inside ``<script id="…">`` of a web page; None if the page has no such script.
    Raises ValueError if the script is there but isn't valid JSON."""
    for script in _SCRIPT.finditer(page):
        found = _SCRIPT_ID.search(script.group(1))
        if found and found.group(1) == script_id:
            return json.loads(script.group(2))
    return None


def page_title(page: str) -> str:
    found = re.search(r"<title[^>]*>(.*?)</title>", page, re.DOTALL | re.IGNORECASE)
    return " ".join(html.unescape(found.group(1)).split()) if found else ""


def html_to_text(value: str | None) -> str | None:
    """'Hits by <b>Olivia</b> &amp; more' -> 'Hits by Olivia & more' (None stays None)."""
    if not value:
        return None
    text = re.sub(r"<br\s*/?>|</p>", "\n", value, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line) or None


# --- Small conversions -----------------------------------------------------------------------

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def iso_seconds(value: str | None) -> float | None:
    """An ISO 8601 duration in seconds: 'PT3M27S' -> 207.0. None if it can't be read."""
    found = _ISO_DURATION.match(value or "")
    if not found or not value or value in ("P", "PT"):
        return None
    parts = {k: float(v) for k, v in found.groupdict(default="0").items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def ms_to_seconds(value: object) -> float | None:
    """Milliseconds (as the services send them) in seconds; None for missing or odd values."""
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return round(value / 1000, 3)
    return None


def require_tracks(tracks: list[SourceTrack], service: str, url: str, **details) -> None:
    """An empty playlist ends the refresh with an error: more likely a problem reading it than
    a playlist that was really emptied, and Navidrome should keep the last good state."""
    if not tracks:
        raise source_error(
            f"The {service} playlist has no tracks Releasomat can read. If it really is empty, "
            "add tracks to it; otherwise the error report shows what came back.",
            url=url,
            **details,
        )


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait, from the Retry-After header (or ListenBrainz's X-RateLimit-Reset-In)."""
    for header in ("Retry-After", "X-RateLimit-Reset-In"):
        try:
            return float(response.headers[header])
        except (KeyError, ValueError):
            continue
    return None
