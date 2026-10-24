"""Soulseek with real answers: five searches and three downloads recorded from the owner's slskd
0.26.1 on 2026-10-23 (ADR 0008), peer names and share paths made anonymous. They guard the
choosing rules against how people really name their files."""

import json
from pathlib import Path

import pytest

from app.downloaders.slskd import (
    DEFAULT_FORMATS,
    Candidate,
    _failure,
    check_candidate,
    choose,
    file_title,
)
from app.sources.base import SourceTrack

ANSWERS = Path(__file__).parent.parent / "fixtures" / "online"
SEARCHES = json.loads((ANSWERS / "slskd_real_searches.json").read_text())["searches"]


def search(name: str) -> tuple[SourceTrack, list[dict]]:
    entry = SEARCHES[name]
    return SourceTrack(**entry["track"]), entry["responses"]


@pytest.mark.parametrize(
    ("stem", "title"),
    [
        ("01 - Harter Street VIP", "Harter Street VIP"),
        ("09. SOTA - Harter Street VIP", "Harter Street VIP"),
        ("[09] Sota - Harter Street VIP", "Harter Street VIP"),
        ("09_sota_-_harter_street_(vip)", "harter street (vip)"),
        ("Sota - Harter Street VIP [www.slider.kz]", "Harter Street VIP"),
        ("Sota - Harter Street VIP (Original Mix)", "Harter Street VIP (Original Mix)"),
        ("Delta Heavy - Vandal - 01 - Vandal", "Vandal"),
        ("Harter Street VIP", "Harter Street VIP"),
        ("Rocker - Darkness", "Rocker - Darkness"),  # no artist in front: left as it is
    ],
)
def test_the_title_part_of_a_file_name(stem, title):
    assert file_title(stem, "SOTA" if "arter" in stem else "Delta Heavy") == title


def test_numbered_and_artist_prefixed_names_are_the_same_version():
    # The album as Apple Music names it: a folder named after it counts instead of the artist.
    track = SourceTrack("SOTA", "Harter Street VIP", album="Harter Street VIP", duration=143)

    def check(name: str):
        c = Candidate("peer", name, 1000, 143, None, 16, True, 0, 1_000_000)
        return check_candidate(track, c, DEFAULT_FORMATS, 320, 85)

    assert check("@@p\\Music\\Harter Street VIP\\01 - Harter Street VIP.flac") is None
    assert check("@@p\\Music\\Sota\\09. Sota - Harter Street VIP.flac") is None
    assert check("@@p\\Music\\VA-Elevate\\09_sota_-_harter_street_(vip).flac") is None
    assert check("@@p\\Music\\DnB\\Sota - Harter Street VIP (Original Mix).flac") is None
    assert check("@@p\\Music\\DnB\\Sota - Harter Street VIP Original Mix.flac") is None
    assert check("@@p\\Music\\Harter Street VIP\\cover.jpg") == (
        "format",
        "not an audio file (JPG)",
    )
    original = SourceTrack("SOTA", "Harter Street", duration=143)
    c = Candidate(
        "peer", "@@p\\Music\\Sota\\01 - Harter Street VIP.flac", 1, 143, None, 16, True, 0, 1
    )
    assert check_candidate(original, c, DEFAULT_FORMATS, 320, 85)[0] == "version"


@pytest.mark.parametrize("name", list(SEARCHES))
def test_every_real_search_has_a_flac_first(name):
    track, responses = search(name)
    choice = choose(track, responses, DEFAULT_FORMATS, 320, 85)
    assert choice.usable, choice.summary()
    assert choice.usable[0].ext == "flac"


def test_the_vip_is_found_in_its_usual_names():
    """Before the fix, 27 of these files counted as "another version" (the part after the dash
    was read as a version)."""
    track, responses = search("SOTA - Harter Street VIP")
    choice = choose(track, responses, DEFAULT_FORMATS, 320, 85)
    usable = [c.name for c in choice.usable]
    assert "01 - Harter Street VIP.flac" in usable
    reasons = [reason for _, kind, reason in choice.rejected if kind == "version"]
    assert not any("harter street" in reason for reason in reasons)


def test_the_remix_is_kept_apart_from_the_original():
    track, responses = search("Simula - Angels (Mefjus Remix)")
    choice = choose(track, responses, DEFAULT_FORMATS, 320, 85)
    assert all("mefjus" in c.filename.lower() for c in choice.usable)


def test_real_transfer_answers():
    created = json.loads((ANSWERS / "slskd_real_batch_created.json").read_text())
    assert created["batch"]["transfers"][0]["id"]
    succeeded = json.loads((ANSWERS / "slskd_real_batch_succeeded.json").read_text())
    assert succeeded["transfers"][0]["state"] == "Completed, Succeeded"
    rejected = json.loads((ANSWERS / "slskd_real_batch_rejected.json").read_text())["transfers"][0]
    assert _failure(rejected["state"], rejected["exception"]) == (
        "the peer refused the download (Transfer rejected: File not shared.)"
    )
    application = json.loads((ANSWERS / "slskd_real_application.json").read_text())
    assert application["version"]["current"].startswith("0.26")
    assert application["server"]["isLoggedIn"] is True
