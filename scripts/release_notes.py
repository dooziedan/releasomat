"""Release notes for one version, cut from CHANGELOG.md, for the GitHub Releases page.

.github/workflows/release.yml runs it when a tag vX.Y.Z is pushed:

    python3 scripts/release_notes.py v0.1.0 > notes.md

Prints that version's section (without its heading) and a line on how to get the image. The
heading may be "## [0.1.0] - 2026-11-01" or "## 0.1.0 - 2026-11-01". Fails with exit code 1 when
the changelog has no section for the version: the entries were probably still under "Unreleased"
when the tag was made.
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
IMAGE = "ghcr.io/dooziedan/releasomat"


def section(changelog: str, version: str) -> str | None:
    """The text under the version's "## " heading, up to the next "## " heading; None if missing."""
    lines = changelog.splitlines()
    heading = re.compile(rf"## \[?v?{re.escape(version)}\]?(\s|$)")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end]).strip() or None


def notes(changelog: str, version: str) -> str | None:
    body = section(changelog, version)
    if body is None:
        return None
    return (
        f"{body}\n\n---\n\n"
        f'**Update:** Unraid shows "update available" for the container. '
        f"The image is `{IMAGE}:{version}`."
    )


def main(args: list[str]) -> int:
    if len(args) != 1:
        print("Usage: release_notes.py vX.Y.Z", file=sys.stderr)
        return 2
    version = args[0].removeprefix("v")
    text = notes(CHANGELOG.read_text(encoding="utf-8"), version)
    if text is None:
        print(
            f"CHANGELOG.md has no section '## [{version}]'. Move the Unreleased entries "
            "to a new version section before tagging.",
            file=sys.stderr,
        )
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
