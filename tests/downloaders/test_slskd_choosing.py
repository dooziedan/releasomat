"""Soulseek: search texts, and which files are taken in which order. The search answer is a saved
one shaped like slskd 0.26.1's (tests/fixtures/online/slskd_search_responses.json)."""

import json
from pathlib import Path

from app.downloaders.slskd import (
    DEFAULT_FORMATS,
    Candidate,
    check_candidate,
    choose,
    clean_url,
    normalise_formats,
    rank,
    search_queries,
)
from app.sources.base import SourceTrack

ANSWERS = Path(__file__).parent.parent / "fixtures" / "online"
GET_LUCKY = SourceTrack(
    "Daft Punk feat. Pharrell Williams & Nile Rodgers", "Get Lucky", "Random Access Memories", 369
)


def responses() -> list[dict]:
    return json.loads((ANSWERS / "slskd_search_responses.json").read_text())


def candidate(name="@@p\\Music\\Daft Punk\\RAM\\08 Get Lucky.flac", **fields) -> Candidate:
    values = {
        "username": "peer",
        "filename": name,
        "size": 1000,
        "length": 369,
        "bit_rate": None,
        "bit_depth": 16,
        "free_slot": True,
        "queue_length": 0,
        "upload_speed": 1_000_000,
    }
    return Candidate(**{**values, **fields})


def test_search_texts_exact_first_then_simpler():
    assert search_queries(SourceTrack("Beyoncé", "Halo")) == ["beyoncé halo", "*eyonce halo"]
    assert search_queries(GET_LUCKY) == ["daft punk get lucky", "*aft punk get lucky"]
    assert search_queries(SourceTrack("The Weeknd", "Blinding Lights")) == [
        "the weeknd blinding lights",
        "the *eeknd blinding lights",
    ]
    # Versions are part of the exact search only; remaster notes and "feat." never are.
    assert search_queries(SourceTrack("SAINt JHN", "Roses (Imanbek Remix)")) == [
        "saint jhn roses imanbek remix",
        "*aint jhn roses",
    ]
    assert search_queries(SourceTrack("Queen", "Bohemian Rhapsody - Remastered 2011")) == [
        "queen bohemian rhapsody",
        "*ueen bohemian rhapsody",
    ]


def test_the_saved_answer_is_sorted_into_usable_and_rejected():
    choice = choose(GET_LUCKY, responses(), DEFAULT_FORMATS, 320, 85)
    assert [(c.username, c.ext) for c in choice.usable] == [
        ("flac_hoarder", "flac"),  # FLAC, free slot, empty queue, fastest
        ("tidy_tagger", "flac"),  # FLAC, but a queue of 5; only the album is in its path
        ("dj_aiff", "aiff"),  # ".aif" counts as AIFF, the second format
        ("mp3_collector", "mp3"),  # MP3 320 last; slskd's empty "extension" doesn't matter
    ]
    reasons = {(c.username, kind) for c, kind, _ in choice.rejected}
    assert reasons == {
        ("hires_vault", "slot"),  # 24 bit, but no free upload slot
        ("radio_rips", "bitrate"),  # 128 kbit/s
        ("itunes_fan", "format"),  # M4A isn't wanted
        ("radio_rips", "length"),  # the radio edit is 4:08, not 6:09
        ("live_bootlegs", "version"),  # (Live)
        ("studio_band", "artist"),  # a cover: neither the artist nor the album in the path
        ("flac_hoarder", "title"),  # another song of the album
    }
    # Locked files ("friends only") are never candidates.
    assert all(c.username != "friends_only" for c in choice.usable)
    # The report lists the right track in the wrong quality first.
    assert [kind for _, kind, _ in choice.rejected][:3] == ["slot", "bitrate", "format"]


def test_the_rejected_files_in_the_report():
    tried = choose(GET_LUCKY, responses(), DEFAULT_FORMATS, 320, 85).tried()
    assert len(tried) == 7  # six one by one, the seventh counted
    assert tried[0] == {
        "downloader": "slskd",
        "candidate": "Soulseek: hires_vault · 08 Get Lucky.flac (FLAC 24 bit, 6:09)",
        "reason": "the peer has no free upload slot (queue: 31)",
    }
    assert tried[1]["reason"] == "128 kbit/s, at least 320 wanted"
    assert tried[-1] == {
        "downloader": "slskd",
        "candidate": "Soulseek: 1 more file",
        "reason": "1 × other songs",
    }


def test_the_format_setting_decides_the_order():
    formats = normalise_formats(["MP3", ".flac"])
    assert formats == ["mp3", "flac"]
    choice = choose(GET_LUCKY, responses(), formats, 320, 85)
    assert [c.username for c in choice.usable] == ["mp3_collector", "flac_hoarder", "tidy_tagger"]
    assert ("dj_aiff", "format") in {(c.username, kind) for c, kind, _ in choice.rejected}
    assert normalise_formats(None) == DEFAULT_FORMATS == ["flac", "aiff", "wav", "mp3"]
    assert normalise_formats(["AIF", "wave"]) == ["aiff", "wav"]


def test_a_lower_minimum_bitrate_lets_more_mp3s_in():
    choice = choose(GET_LUCKY, responses(), DEFAULT_FORMATS, 128, 85)
    assert ("radio_rips", "mp3") in [(c.username, c.ext) for c in choice.usable]


def test_ranking_format_then_quality_then_queue_and_speed():
    formats = DEFAULT_FORMATS
    flac16 = candidate(bit_depth=16)
    flac24 = candidate(bit_depth=24)
    busy = candidate(bit_depth=24, queue_length=3)
    slow = candidate(bit_depth=24, upload_speed=10)
    mp3_320 = candidate("@@p\\Daft Punk - Get Lucky.mp3", bit_rate=320, bit_depth=None)
    mp3_unknown = candidate("@@p\\Daft Punk - Get Lucky.mp3", bit_rate=None, bit_depth=None)
    ordered = sorted(
        [mp3_unknown, slow, mp3_320, flac16, busy, flac24], key=lambda c: rank(c, formats)
    )
    assert ordered == [flac24, slow, busy, flac16, mp3_320, mp3_unknown]


def test_lossy_files_without_a_bitrate_are_allowed_and_checked_after_the_download():
    track = SourceTrack("Daft Punk", "Get Lucky", duration=369)
    unknown = candidate("@@p\\Daft Punk - Get Lucky.mp3", bit_rate=None, bit_depth=None)
    assert check_candidate(track, unknown, DEFAULT_FORMATS, 320, 85) is None
    low = candidate("@@p\\Daft Punk - Get Lucky.mp3", bit_rate=256, bit_depth=None)
    assert check_candidate(track, low, DEFAULT_FORMATS, 320, 85) == (
        "bitrate",
        "256 kbit/s, at least 320 wanted",
    )
    # Lossless files have no minimum bitrate.
    assert check_candidate(track, candidate(bit_rate=200), DEFAULT_FORMATS, 320, 85) is None


def test_the_path_must_name_the_title_and_the_artist_or_album():
    track = SourceTrack("Beyoncé", "Halo", "I Am... Sasha Fierce", 261)

    def check(path):
        return check_candidate(track, candidate(path, length=261), DEFAULT_FORMATS, 320, 85)

    assert check("@@p\\Music\\Beyonce\\I Am... Sasha Fierce\\02 Halo.flac") is None
    assert check("@@p\\Music\\Various\\I Am Sasha Fierce\\02 Halo.flac") is None  # the album
    assert check("@@p\\Music\\Beyoncé - Halo.flac") is None  # accents don't matter
    assert check("@@p\\Music\\Depeche Mode\\Violator\\04 Halo.flac")[0] == "artist"
    assert check("@@p\\Music\\Beyonce\\02 If I Were a Boy.flac")[0] == "title"
    assert check("@@p\\Music\\Beyonce\\02 Halo (Acoustic).flac")[0] == "version"
    assert check("@@p\\Beyonce - Live at Wembley\\Halo.flac")[0] == "version"  # the folder


def test_versions_must_match():
    remix = SourceTrack("SAINt JHN", "Roses (Imanbek Remix)", duration=176)

    def check(track, path):
        c = candidate(path, length=track.duration)
        return check_candidate(track, c, DEFAULT_FORMATS, 320, 85)

    assert check(remix, "@@p\\SAINt JHN - Roses (Imanbek Remix).flac") is None
    assert check(remix, "@@p\\Roses (Imanbek Remix) - Single\\01 SAINt JHN - Roses.flac") is None
    assert check(remix, "@@p\\SAINt JHN - Roses.flac") == ("version", "not the imanbek remix")
    original = SourceTrack("SAINt JHN", "Roses", duration=176)
    assert check(original, "@@p\\SAINt JHN - Roses (Imanbek Remix).flac")[0] == "version"
    assert check(original, "@@p\\SAINt JHN - Roses (Dub Mix).flac") == (
        "version",
        "another version (dub mix)",
    )
    assert check(original, "@@p\\SAINt JHN - Roses (Radio Edit).flac") is None  # a soft note


def test_lengths_within_ten_seconds():
    track = SourceTrack("Daft Punk", "Get Lucky", duration=369)
    assert check_candidate(track, candidate(length=378), DEFAULT_FORMATS, 320, 85) is None
    assert check_candidate(track, candidate(length=381), DEFAULT_FORMATS, 320, 85)[0] == "length"
    assert check_candidate(track, candidate(length=None), DEFAULT_FORMATS, 320, 85) is None
    unknown = SourceTrack("Daft Punk", "Get Lucky")
    assert check_candidate(unknown, candidate(length=100), DEFAULT_FORMATS, 320, 85) is None


def test_one_file_per_peer_its_best():
    track = SourceTrack("Daft Punk", "Get Lucky", duration=369)
    answer = [
        {
            "username": "twofiles",
            "hasFreeUploadSlot": True,
            "queueLength": 0,
            "uploadSpeed": 100,
            "files": [
                {
                    "filename": "@@t\\Daft Punk\\Get Lucky.mp3",
                    "size": 5,
                    "bitRate": 320,
                    "length": 369,
                },
                {
                    "filename": "@@t\\Daft Punk\\Get Lucky.flac",
                    "size": 9,
                    "bitDepth": 16,
                    "length": 369,
                },
            ],  # fmt: skip
        }
    ]
    choice = choose(track, answer, DEFAULT_FORMATS, 320, 85)
    assert [(c.username, c.ext) for c in choice.usable] == [("twofiles", "flac")]


def test_addresses_are_cleaned():
    assert clean_url("192.168.1.5:5030/") == "http://192.168.1.5:5030"
    assert clean_url(" https://slskd.example.org/api/v0 ") == "https://slskd.example.org"
    assert clean_url("") == "" and clean_url(None) == ""
