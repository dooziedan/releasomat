"""Settings: connections to Navidrome, slskd, ListenBrainz and Tidal, the download sources and
download defaults.

Fields set by an env var are shown read-only ("set by the container"). Secrets are never sent
back to the browser: their fields stay empty, and leaving them empty keeps the saved value.

"List" settings (the download sources, Soulseek's formats) are rows with a switch and ▲/▼
buttons. The buttons ask the server for the list in its new order (htmx, nothing is saved yet);
"Save settings" saves it with everything else. Without JavaScript a ▲/▼ button sends the whole
form, which saves it in the new order.
"""

import logging
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app import reports
from app import settings as user_settings
from app.config import ConfigDep, get_config
from app.db import SessionDep
from app.downloaders import slskd
from app.downloaders.base import DownloaderError
from app.navidrome import Navidrome, PingResult
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class CheckResult:
    """What a Test button shows: whether it worked, and the service's answer in plain words."""

    ok: bool
    message: str


@router.get("/api/settings", tags=["settings"])
def get_settings(session: SessionDep) -> list[user_settings.View]:
    """Every user setting with where its value comes from. Secrets are masked."""
    return user_settings.all_views(session)


@router.put("/api/settings", tags=["settings"])
def put_settings(changes: dict[str, str], session: SessionDep) -> list[user_settings.View]:
    """Save the settings that are sent ({"NAVIDROME_URL": "http://…"}); an empty value goes back
    to the default. 409 for a setting an env var sets, 422 for unknown names or bad values."""
    unknown = [key for key in changes if key not in user_settings.BY_KEY]
    if unknown:
        raise HTTPException(422, f"Unknown setting {unknown[0]}")
    locked = [key for key in changes if user_settings.is_locked(key)]
    if locked:
        raise HTTPException(409, f"{locked[0]} is set by the container and can't be changed here")
    errors = _check(changes)
    if errors:
        raise HTTPException(422, errors)
    for key, value in changes.items():
        user_settings.save(session, key, value)
    return user_settings.all_views(session)


@router.post("/api/navidrome/test", tags=["settings"])
async def test_navidrome(session: SessionDep) -> PingResult:
    """Log in to Navidrome with the saved settings and report what it answered."""
    return await Navidrome.from_settings(session).ping()


@router.post("/api/slskd/test", tags=["settings"])
async def test_slskd(session: SessionDep) -> CheckResult:
    """Ask slskd (Soulseek) with the saved address and API key whether it answers."""
    url = user_settings.get(session, "SLSKD_URL")
    api_key = user_settings.get(session, "SLSKD_API_KEY")
    return await check_slskd(url, api_key)


async def check_slskd(url: str, api_key: str) -> CheckResult:
    """Try slskd's address and API key. The key never appears in the message."""
    if not url or not api_key:
        return CheckResult(False, "Enter slskd's address and API key first.")
    try:
        url = user_settings.clean(user_settings.BY_KEY["SLSKD_URL"], url)
        message = await slskd.check_connection(url, api_key)
        result = CheckResult(True, message)
    except user_settings.InvalidSetting as exc:
        result = CheckResult(False, f"Address: {exc}.")
    except DownloaderError as exc:
        result = CheckResult(False, str(exc))
    except Exception as exc:  # whatever happens, the button shows an answer
        log.warning("Testing slskd failed: %s", type(exc).__name__)
        result = CheckResult(False, f"Unexpected error: {type(exc).__name__}")
    result.message = reports.scrub(result.message, [api_key] if len(api_key) >= 4 else [])
    return result


def _check(changes: dict[str, str]) -> dict[str, str]:
    errors = {}
    for key, value in changes.items():
        try:
            user_settings.clean(user_settings.BY_KEY[key], value)
        except user_settings.InvalidSetting as exc:
            errors[key] = str(exc)
    return errors


def _page(request, session, config, errors=None, submitted=None, status_code=200):
    views = user_settings.all_views(session)
    groups = {
        name: [v for v in views if v.group == key] for key, name in user_settings.GROUPS.items()
    }
    errors, submitted = errors or {}, submitted or {}
    lists = {}
    for definition in user_settings.DEFINITIONS:
        if definition.kind == "list":
            key = definition.key
            enabled = None
            if key in submitted:
                enabled = user_settings.names_of(submitted[key] or definition.default)
            lists[key] = _list(session, key, enabled, errors.get(key))
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "page": "settings",
            "groups": groups,
            "group_ids": {name: key for key, name in user_settings.GROUPS.items()},
            "lists": lists,
            "errors": errors,
            "submitted": submitted,
            "config": config,
            "versions": reports.versions(),
            "cookies": (config.config_dir / "cookies.txt").is_file(),
        },
        status_code=status_code,
    )


# --- "List" settings: rows with a switch and ▲/▼ buttons ------------------------------------


def _list(
    session: Session, key: str, enabled: list[str] | None = None, error: str | None = None
) -> dict:
    """What the page shows for a "list" setting: the switched-on entries in their order (numbered),
    then the switched-off ones. ``enabled`` is the order on the page, if it isn't saved yet."""
    definition = user_settings.BY_KEY[key]
    saved = user_settings.get_list(session, key)
    locked = user_settings.is_locked(key)
    if enabled is None or locked:
        enabled = saved
    slskd_ready = user_settings.slskd_set_up(session)
    rows = []
    off = [name for name in definition.choices if name not in enabled]
    for name in enabled + off:
        on = name in enabled
        rows.append(
            {
                "name": name,
                "label": user_settings.CHOICE_LABELS.get(name, name),
                "on": on,
                "position": enabled.index(name) + 1 if on else None,
                "first": on and name == enabled[0],
                "last": on and name == enabled[-1],
                **_hint(name, slskd_ready),
            }
        )
    summary = ""
    if key == "DOWNLOAD_SOURCES":
        words = user_settings.describe_sources(enabled, slskd_ready)
        summary = (
            f"Missing tracks are downloaded with {words}."
            if words
            else "No source is switched on: Releasomat only links songs that are already in "
            "Navidrome."
        )
    return {
        "key": key,
        "label": definition.label,
        "rows": rows,
        "locked": locked,
        "unsaved": enabled != saved,
        "summary": summary,
        "error": error,
    }


def _hint(name: str, slskd_ready: bool) -> dict:
    """The status shown next to a download source or a format."""
    if name == "slskd":
        if slskd_ready:
            return {"badge": "set up", "badge_class": "ok", "hint": "Test it below."}
        return {
            "badge": "not connected",
            "badge_class": "warn",
            "hint": "Add the address and API key below.",
        }
    if name == "youtube":
        return {"badge": "ready", "badge_class": "ok", "hint": "Built in."}
    if name in user_settings.LOSSLESS:
        return {"badge": "lossless", "badge_class": "", "hint": ""}
    return {"badge": "lossy", "badge_class": "", "hint": ""}


def _enabled(form, key: str) -> list[str]:
    """The switched-on entries of a list, in the order the page shows them."""
    choices = user_settings.BY_KEY[key].choices
    names = []
    for value in form.getlist(key):
        name = str(value)
        if name in choices and name not in names:
            names.append(name)
    return names


def _moved(names: list[str], key: str, move: str) -> list[str]:
    """Apply a ▲/▼ button ("DOWNLOAD_SOURCES:youtube:up") to the list ``key``."""
    move_key, _, rest = move.partition(":")
    name, _, direction = rest.partition(":")
    if move_key != key or name not in names:
        return names
    names = list(names)
    i = names.index(name)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(names):
        names[i], names[j] = names[j], names[i]
    return names


@router.post("/partials/settings-list/{key}", response_class=HTMLResponse, include_in_schema=False)
async def settings_list_partial(key: str, request: Request, session: SessionDep):
    """A list setting again, after a switch or a ▲/▼ button (htmx). Nothing is saved here."""
    definition = user_settings.BY_KEY.get(key)
    if definition is None or definition.kind != "list":
        raise HTTPException(404, "No such list setting")
    form = await request.form()
    enabled = _moved(_enabled(form, key), key, str(form.get("move", "")))
    return templates.TemplateResponse(
        request, "partials/order_list.html", {"l": _list(session, key, enabled)}
    )


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_page(request: Request, session: SessionDep, config: ConfigDep):
    return _page(request, session, config)


@router.post("/settings", response_class=HTMLResponse, include_in_schema=False)
async def save_settings_form(request: Request, session: SessionDep):
    """The whole Settings form. Locked fields are not sent (disabled inputs); a secret's field
    left empty keeps the saved secret, its "Remove" box clears it."""
    form = await request.form()
    move = str(form.get("move", ""))  # a ▲/▼ button pressed without JavaScript
    changes = {}
    for definition in user_settings.DEFINITIONS:
        key = definition.key
        if user_settings.is_locked(key):
            continue
        value = str(form.get(key, "")).strip()
        if definition.kind == "list":
            # Switches that are off send nothing; the form says which lists it showed.
            if form.get(f"shown_{key}"):
                names = _moved(_enabled(form, key), key, move)
                value = ",".join(names) or user_settings.NONE
                changes[key] = "" if value == definition.default else value
            continue
        if definition.kind == "bool":
            # A switch that is off sends nothing; the form says which switches it showed.
            if form.get(f"shown_{key}"):
                changes[key] = "true" if value else "false"
            continue
        if definition.secret:
            if form.get(f"clear_{key}"):
                changes[key] = ""
            elif value:
                changes[key] = value
        elif key in form:
            changes[key] = value
    errors = _check(changes)
    if errors:
        submitted = {k: v for k, v in changes.items() if not user_settings.BY_KEY[k].secret}
        return _page(request, session, get_config(), errors, submitted, status_code=422)
    for key, value in changes.items():
        user_settings.save(session, key, value)
    moved_key = move.partition(":")[0]
    anchor = f"#list-{moved_key}" if moved_key in user_settings.BY_KEY else ""
    return RedirectResponse(f"/settings?saved=1{anchor}", status_code=303)


@router.post("/partials/navidrome-test", response_class=HTMLResponse, include_in_schema=False)
async def navidrome_test_partial(request: Request, session: SessionDep):
    """The Test button: tries the values in the form (not saved yet), falling back to the saved
    ones for fields left empty (like the password)."""
    form = await request.form()

    def value(key: str) -> str:
        typed = str(form.get(key, "")).strip()
        if typed and not user_settings.is_locked(key):
            return typed
        return user_settings.get(session, key)

    navidrome = Navidrome(
        value("NAVIDROME_URL"), value("NAVIDROME_USER"), value("NAVIDROME_PASSWORD")
    )
    result = await navidrome.ping()
    return templates.TemplateResponse(request, "partials/test_result.html", {"result": result})


@router.post("/partials/slskd-test", response_class=HTMLResponse, include_in_schema=False)
async def slskd_test_partial(request: Request, session: SessionDep):
    """Soulseek's Test button: the address and API key in the form, or the saved ones."""
    form = await request.form()

    def value(key: str) -> str:
        typed = str(form.get(key, "")).strip()
        if typed and not user_settings.is_locked(key):
            return typed
        return user_settings.get(session, key)

    result = await check_slskd(value("SLSKD_URL"), value("SLSKD_API_KEY"))
    return templates.TemplateResponse(request, "partials/test_result.html", {"result": result})
