"""Normalising names and scoring "is this the same track?"."""

from dataclasses import dataclass

import pytest

from app.matching import (
    TitleParts,
    artist_score,
    channel_artist,
    is_match,
    main_artist,
    match_song,
    normalise,
    same_versions,
    search_text,
    similarity,
    split_artists,
    title_parts,
    title_score,
    track_key,
    track_score,
    unwanted_words,
    video_score,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Beyoncé", "beyonce"),
        ("Sigur Rós", "sigur ros"),
        ("Get Lucky (feat. Pharrell Williams)", "get lucky"),
        ("Get Lucky [ft. Pharrell Williams]", "get lucky"),
        ("Daft Punk feat. Pharrell Williams", "daft punk"),
        ("Daft Punk Featuring Pharrell Williams", "daft punk"),
        ("Song (Remastered 2011)", "song"),
        ("Song [2011 Remaster]", "song"),
        ("Song - Remastered 2009", "song"),
        ("Song - 2009 Remastered Version", "song"),
        ("Simon & Garfunkel", "simon and garfunkel"),
        ("Don't Stop Me Now!", "don t stop me now"),
        ("  AC/DC  ", "ac dc"),
        ("Strobe (Extended Mix)", "strobe extended mix"),  # mix names stay: DJs care
        ("Feat of Strength", "feat of strength"),  # "feat" at the start is a word
        ("Aftermath", "aftermath"),  # "ft" inside a word stays
        (None, ""),
    ],
)
def test_normalise(text, expected):
    assert normalise(text) == expected


def test_similarity():
    assert similarity("Beyoncé", "BEYONCE") == 100
    assert similarity("Get Lucky (feat. Pharrell)", "Get Lucky - Remastered 2013") == 100
    assert similarity("One More Time", "Time More One") == 100  # word order doesn't matter
    assert similarity("Strobe", "Ghosts n Stuff") < 50
    assert similarity("", "x") == 0


def test_track_score():
    same = track_score("Daft Punk", "Get Lucky", "Daft Punk, Pharrell Williams", "Get Lucky")
    assert same == 100
    assert is_match(same, 85)
    other = track_score("Daft Punk", "Get Lucky", "Deadmau5", "Strobe")
    assert other < 50 and not is_match(other, 85)
    radio_vs_extended = track_score("deadmau5", "Strobe", "deadmau5", "Strobe (Extended Mix)")
    assert radio_vs_extended < 100


def test_very_different_lengths_cost_points():
    close = track_score("A", "Song", "A", "Song", 200, 205)
    far = track_score("A", "Song", "A", "Song", 200, 600)
    assert close == 100 and far == 70


# --- Songs and versions ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "song", "versions"),
    [
        ("Strobe (Extended Mix)", "strobe", ("extended mix",)),
        ("Strobe - Radio Edit", "strobe", ()),  # a radio edit is the usual version
        ("Strobe (Original Mix)", "strobe", ()),  # Beatport's name for the plain version
        ("Bohemian Rhapsody - Remastered 2011", "bohemian rhapsody", ()),
        ("Uptown Funk (feat. Bruno Mars)", "uptown funk", ()),
        ("Roses (Imanbek Remix)", "roses", ("imanbek remix",)),
        ("Song - Live at Wembley 1986", "song", ("live at wembley 1986",)),
        ("Love Story (Taylor's Version)", "love story", ("taylor s version",)),
        ("Song [Remastered 2009] (Club Mix)", "song", ("club mix",)),
        ("Song (Part 2)", "song part 2", ()),  # part of the name, not a version
        ("(I Can't Get No) Satisfaction", "i can t get no satisfaction", ()),
        ('Song - From "Top Gun: Maverick"', "song", ()),
        ("Mix Tape", "mix tape", ()),  # "mix" outside brackets is just a word
        ("Get Lucky (Official Audio) ft. Pharrell Williams", "get lucky", ()),
    ],
)
def test_title_parts(title, song, versions):
    assert title_parts(title) == TitleParts(song, versions)


def test_mix_names_stay_apart():
    """For a DJ, the extended mix and the radio edit are different tracks."""
    assert title_score("Strobe (Extended Mix)", "Strobe (Radio Edit)") == 0
    assert title_score("Strobe (Extended Mix)", "Strobe - Extended Mix") == 100
    assert title_score("Strobe", "Strobe (Club Mix)") == 0
    assert title_score("Strobe", "Strobe - Radio Edit") == 100
    assert title_score("Roses", "Roses (Imanbek Remix)") == 0
    assert same_versions(("imanbek remix",), ("imanbek remix",))


def test_artists():
    assert split_artists("Daft Punk feat. Pharrell Williams & Nile Rodgers") == [
        "daft punk",
        "pharrell williams",
        "nile rodgers",
    ]
    assert main_artist("Mark Ronson, Bruno Mars") == "mark ronson"
    assert artist_score("Daft Punk", "Daft Punk feat. Pharrell Williams") == 100
    assert artist_score("Simon & Garfunkel", "Simon and Garfunkel") == 100
    assert artist_score("Beyonce", ["Beyoncé"]) == 100
    assert artist_score("Queen", "Queen Latifah") < 85  # one shared word is not enough
    assert artist_score("Beyoncé", "Depeche Mode") < 50


@dataclass
class Wanted:
    artist: str
    title: str
    duration: float | None = None
    isrc: str | None = None
    mbid: str | None = None


def nd_song(id, artist, title, duration, **extra):
    """A song as Navidrome's search3 lists it (the demo library's tags and lengths)."""
    return {"id": id, "artist": artist, "title": title, "duration": duration, **extra}


DEMO = [
    nd_song("bo", "Queen", "Bohemian Rhapsody - Remastered 2011", 355),
    nd_song("ds", "Queen", "Don't Stop Me Now", 209),
    nd_song("gl", "Daft Punk feat. Pharrell Williams & Nile Rodgers", "Get Lucky", 369,
            artists=[{"name": "Daft Punk"}, {"name": "Pharrell Williams"}]),
    nd_song("uf", "Mark Ronson", "Uptown Funk (feat. Bruno Mars)", 270),
    nd_song("hb", "Beyoncé", "Halo", 261),
    nd_song("hd", "Depeche Mode", "Halo", 270),
    nd_song("mh", "Motörhead", "Ace of Spades", 169),
    nd_song("sr", "Sigur Rós", "Hoppípolla", 268),
    nd_song("sg", "Simon and Garfunkel", "The Sound of Silence", 186),
    nd_song("ew", "Earth, Wind & Fire", "September", 215),
    nd_song("ro", "SAINt JHN", "Roses (Imanbek Remix)", 176),
    nd_song("hg", "Herbert Grönemeyer", "Männer", 238),
    nd_song("jc", "Johnny Cash", "Hurt", 218),
    nd_song("ni", "Nine Inch Nails", "Hurt", 373),
]  # fmt: skip


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        (Wanted("Queen", "Bohemian Rhapsody", 354), "bo"),
        (Wanted("Queen", "Don't Stop Me Now - Remastered 2011", 209), "ds"),
        (Wanted("Daft Punk", "Get Lucky (feat. Pharrell Williams & Nile Rodgers)", 369), "gl"),
        (Wanted("Daft Punk", "Get Lucky (Radio Edit)", 248), None),  # 2 minutes shorter
        (Wanted("Mark Ronson, Bruno Mars", "Uptown Funk", 270), "uf"),
        (Wanted("Beyonce", "Halo", 261), "hb"),
        (Wanted("Depeche Mode", "Halo", 270), "hd"),
        (Wanted("Motorhead", "Ace Of Spades", 167), "mh"),
        (Wanted("Sigur Ros", "Hoppipolla", 268), "sr"),
        (Wanted("Simon & Garfunkel", "The Sound of Silence", 187), "sg"),
        (Wanted("Earth, Wind & Fire", "September", 215), "ew"),
        (Wanted("SAINt JHN", "Roses", 171), None),  # the remix is another track
        (Wanted("SAINt JHN", "Roses - Imanbek Remix", 176), "ro"),
        (Wanted("Herbert Groenemeyer", "Männer", 237), "hg"),
        (Wanted("Johnny Cash", "Hurt", None), "jc"),
        (Wanted("Nine Inch Nails", "Hurt", 373), "ni"),
        (Wanted("Rammstein", "Du hast", 234), None),
    ],
)
def test_the_demo_librarys_tricky_cases(wanted, expected):
    found = match_song(wanted, DEMO, 85)
    assert (found.song["id"] if found else None) == expected


def test_exact_ids_win():
    mbid = "1a2b3c4d-1234-4abc-8def-0123456789ab"
    songs = [nd_song("x", "Other", "Other", 100, musicBrainzId=mbid)]
    assert match_song(Wanted("A", "B", 300, mbid=mbid), songs, 85).method == "mbid"
    songs = [nd_song("y", "deadmau5", "Strobe", 600, isrc=["CAN111000100"])]
    found = match_song(Wanted("deadmau5", "Strobe", 637, isrc="can111000100"), songs, 85)
    assert found.method == "isrc"  # the same recording, even though the lengths differ


def test_track_key_finds_the_same_track_again():
    assert track_key("Daft Punk", "Get Lucky (feat. Pharrell)") == "daft punk|get lucky|"
    assert track_key("Daft Punk feat. Pharrell", "Get Lucky") == "daft punk|get lucky|"
    assert track_key("deadmau5", "Strobe (Extended Mix)") == "deadmau5|strobe|extended mix"
    assert search_text("Beyoncé", "Halo (Live)") == "beyonce halo live"


# --- YouTube results ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "channel", "good"),
    [
        ("Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams", "Daft Punk", True),
        ("Get Lucky", "Daft Punk - Topic", True),
        ("Get Lucky - Daft Punk", "someone", True),
        ("Daft Punk Get Lucky lyrics", "Lyrics Channel", True),
        ("Daft Punk - Get Lucky (Live at the Grammys)", "DaftPunkVEVO", False),
        ("Pharrell - Happy", "Pharrell", False),
    ],
)
def test_video_score(title, channel, good):
    assert (video_score("Daft Punk", "Get Lucky", title, channel) >= 85) is good


def test_unwanted_words_unless_wanted():
    assert unwanted_words("Daft Punk", "Get Lucky", "Get Lucky Live 2014") == {"live"}
    assert unwanted_words("Daft Punk", "Get Lucky (Live)", "Get Lucky Live 2014") == set()
    assert unwanted_words("A", "Song (Extended Mix)", "Song (Extended Mix)") == set()
    assert unwanted_words("A", "Song", "Song (Karaoke Version)") == {"karaoke"}
    assert channel_artist("Daft Punk - Topic") == "Daft Punk"
    assert channel_artist("DaftPunkVEVO") == "DaftPunk"
