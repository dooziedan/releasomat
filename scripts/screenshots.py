"""Screenshot Releasomat's main pages in Chromium, Firefox and WebKit (Safari's engine).

Every page at desktop and phone width, in the light and the dark look (Pico CSS follows the
browser's setting). Problems are listed at the end and make the script fail: JavaScript errors,
and pages or files (CSS, scripts, htmx partials) that answer with an error.

Point it at a running Releasomat (see docs/development.md):

    .venv/bin/python scripts/screenshots.py                          # http://localhost:8430
    .venv/bin/python scripts/screenshots.py http://localhost:8431    # the dev container

That needs Playwright's browsers once: `.venv/bin/playwright install chromium firefox webkit`.
WebKit may not run on every Linux; Playwright's official Docker image has everything and works
without installing anything on the computer:

    docker run --rm --network host -v "$PWD:/work" -w /work \\
        --user "$(id -u):$(id -g)" -e HOME=/tmp \\
        mcr.microsoft.com/playwright/python:v1.63.0-noble \\
        sh -c "pip install -q --user playwright==1.63.0 && python scripts/screenshots.py"

(--user makes the screenshots belong to you instead of root.)

The screenshots land in dev/screenshots/<date and time>/.
"""

import argparse
import contextlib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
ENGINES = ("chromium", "firefox", "webkit")
SCHEMES = ("light", "dark")
DEVICES = {"desktop": {"width": 1280, "height": 900}, "phone": {"width": 390, "height": 844}}
PAGES = {
    "playlists": "/",
    "playlist-new": "/playlists/new",
    "activity": "/activity",
    "dont-like": "/dont-like",
    "settings": "/settings",
}


def reachable(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/", timeout=5):
            return True
    except urllib.error.HTTPError:
        return True  # it answers, even if with an error; the screenshots will show it
    except OSError:
        return False


def pages(base: str) -> dict[str, str]:
    """The main pages, plus the first playlist's page (tracks, error reports) if there is one."""
    found = dict(PAGES)
    with contextlib.suppress(OSError, ValueError, IndexError, KeyError):
        with urllib.request.urlopen(base + "/api/playlists", timeout=5) as answer:
            first = json.load(answer)[0]
        found["playlist"] = f"/playlists/{first['id']}"
    return found


def open_page(page, url: str) -> None:
    """Open a page and wait until it has settled."""
    page.goto(url, wait_until="load")
    # htmx loads parts of some pages after "load". The activity page keeps a live connection
    # (Server-Sent Events) open, so the network never goes fully quiet there: don't wait long.
    with contextlib.suppress(PlaywrightError):
        page.wait_for_load_state("networkidle", timeout=3000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base_url", nargs="?", default="http://localhost:8430")
    parser.add_argument("--out", type=Path, help="folder for the screenshots")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    out = args.out or ROOT / "dev" / "screenshots" / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    if not reachable(base):
        print(f"Releasomat doesn't answer at {base}. Start it first (docs/development.md).")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    to_capture = pages(base)

    errors: list[str] = []
    count = 0
    with sync_playwright() as p:
        for engine in ENGINES:
            try:
                browser = getattr(p, engine).launch()
            except PlaywrightError as e:
                errors.append(f"{engine}: can't start the browser ({str(e).splitlines()[0]})")
                continue
            for scheme in SCHEMES:
                for device, viewport in DEVICES.items():
                    where = f"{engine}/{scheme}/{device}"
                    context = browser.new_context(viewport=viewport, color_scheme=scheme)
                    page = context.new_page()
                    page.on("pageerror", lambda e, where=where: errors.append(f"{where}: {e}"))
                    page.on(
                        "response",
                        lambda r, where=where: (
                            errors.append(f"{where}: HTTP {r.status} for {r.url}")
                            if r.status >= 400
                            else None
                        ),
                    )
                    for name, path in to_capture.items():
                        open_page(page, base + path)
                        page.screenshot(
                            path=out / f"{name}-{scheme}-{device}-{engine}.png",
                            full_page=device == "desktop",
                        )
                        count += 1
                    context.close()
            browser.close()
            print(f"{engine:9} done", flush=True)

    print(f"{count} screenshots in {out}")
    for error in errors:
        print("ERROR", error)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
