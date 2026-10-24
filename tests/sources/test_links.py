"""Recognising playlist links of every source (no network)."""

import pytest

from app.sources import SOURCES, get_source, label
from app.sources.base import PlaylistRef, Source, UnsupportedLink, detect_source
from app.sources.listenbrainz import SERIES, series_ref, series_title

MBID = "1a2b3c4d-1234-4abc-8def-0123456789ab"
TIDAL = "36ea71a8-445e-41a4-82ab-6628c581535d"


@pytest.mark.parametrize(
    ("link", "source", "playlist_id", "url"),
    [
        # Apple Music: storefront, slug, shared (pl.u-) and editorial playlists
        (
            "https://music.apple.com/de/playlist/feel-good-hits/pl.u-AkAmPlyUxXXXxx",
            "apple_music", "pl.u-AkAmPlyUxXXXxx",
            "https://music.apple.com/de/playlist/feel-good-hits/pl.u-AkAmPlyUxXXXxx",
        ),
        ("https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb?l=en",
         "apple_music", "pl.f4d106fed2bd41149aaacabb233eb5eb", None),
        ("music.apple.com/gb/playlist/pl.u-8aAVZAEFxyz", "apple_music", "pl.u-8aAVZAEFxyz",
         "https://music.apple.com/gb/playlist/pl.u-8aAVZAEFxyz"),
        ("https://embed.music.apple.com/de/playlist/x/pl.u-AkAmPlyUxXXXxx", "apple_music",
         "pl.u-AkAmPlyUxXXXxx", None),
        # Deezer
        ("https://www.deezer.com/de/playlist/908622995", "deezer", "908622995",
         "https://www.deezer.com/playlist/908622995"),
        ("deezer.com/playlist/908622995?utm_campaign=share", "deezer", "908622995", None),
        # Tidal: the web player, the website and its /browse/ links
        (f"https://tidal.com/playlist/{TIDAL}", "tidal", TIDAL, f"https://tidal.com/playlist/{TIDAL}"),
        (f"https://listen.tidal.com/playlist/{TIDAL}?u", "tidal", TIDAL,
         f"https://tidal.com/playlist/{TIDAL}"),
        (f"https://tidal.com/browse/playlist/{TIDAL.upper()}", "tidal", TIDAL, None),
        # YouTube Music and YouTube
        ("https://music.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG&si=AbC",
         "youtube_music", "PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
         "https://music.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
         "youtube_music", "PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", None),
        ("https://music.youtube.com/browse/VLPLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", "youtube_music",
         "PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG", None),
        ("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI",
         "youtube", "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI",
         "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI&index=3",
         "youtube", "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", None),
        ("https://youtu.be/dQw4w9WgXcQ?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", "youtube",
         "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI", None),
        # SoundCloud: sets, mobile host, private sets with their secret token
        ("https://soundcloud.com/some-dj/sets/summer-mix-2026?si=abc&utm_medium=text", "soundcloud",
         "some-dj/sets/summer-mix-2026", "https://soundcloud.com/some-dj/sets/summer-mix-2026"),
        ("https://m.soundcloud.com/some-dj/sets/summer-mix-2026", "soundcloud",
         "some-dj/sets/summer-mix-2026", None),
        ("https://soundcloud.com/some-dj/sets/private-one/s-AbCdEf123", "soundcloud",
         "some-dj/sets/private-one/s-AbCdEf123", None),
        # ListenBrainz
        (f"https://listenbrainz.org/playlist/{MBID}", "listenbrainz", MBID,
         f"https://listenbrainz.org/playlist/{MBID}/"),
        (f"https://listenbrainz.org/playlist/{MBID.upper()}/", "listenbrainz", MBID, None),
        (f"https://beta.listenbrainz.org/playlist/{MBID}/", "listenbrainz", MBID, None),
    ],
)  # fmt: skip
def test_playlist_links_are_recognised(link, source, playlist_id, url):
    ref = detect_source(link)
    assert (ref.source, ref.id, ref.kind) == (source, playlist_id, "playlist")
    if url:
        assert ref.url == url  # cleaned up: no ?si=… or other tracking
    assert ref.label.endswith("playlist")


@pytest.mark.parametrize(
    ("link", "playlist_id", "url", "label"),
    [
        ("https://www.beatport.com/top-100", "top-100", "https://www.beatport.com/top-100",
         "Beatport Top 100"),
        ("beatport.com/hype-100?page=1", "hype-100", "https://www.beatport.com/hype-100",
         "Beatport Hype 100"),
        ("https://www.beatport.com/de/top-100", "top-100", None, "Beatport Top 100"),
        ("https://www.beatport.com/genre/house/5/top-100", "genre/5/top-100",
         "https://www.beatport.com/genre/house/5/top-100", "Beatport genre Top 100"),
        ("https://www.beatport.com/genre/melodic-house-techno/90/hype-100?per_page=50",
         "genre/90/hype-100", "https://www.beatport.com/genre/melodic-house-techno/90/hype-100",
         "Beatport genre Hype 100"),
        ("https://www.beatport.com/chart/party-jumpin-chart/906471", "chart/906471",
         "https://www.beatport.com/chart/party-jumpin-chart/906471", "Beatport chart"),
    ],
)  # fmt: skip
def test_beatport_charts_and_lists_are_recognised(link, playlist_id, url, label):
    ref = detect_source(link)
    assert (ref.source, ref.id, ref.kind, ref.label) == ("beatport", playlist_id, "playlist", label)
    if url:
        assert ref.url == url


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        ("", "Paste a link"),
        ("hello world", "doesn't look like a link"),
        ("https://example.com/playlist/123", "doesn't know example.com"),
        # Spotify is not a source (ADR 0006)
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "doesn't know open.spotify"),
        ("spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "doesn't know spotify.com"),
        ("https://music.apple.com/de/album/random-access-memories/617154241", "an album"),
        ("https://music.apple.com/library/playlist/p.abcdef", "your own library"),
        ("https://www.deezer.com/de/album/302127", "an album on Deezer"),
        ("https://link.deezer.com/s/30ABCdef", "Short Deezer links"),
        (f"https://tidal.com/browse/album/{TIDAL}", "an album on Tidal"),
        ("https://listen.tidal.com/track/77646169", "a single track on Tidal"),
        ("https://tidal.com/browse/artist/8847", "an artist on Tidal"),
        ("https://listen.tidal.com/mix/0123456789abcdef", "a Tidal mix"),
        ("https://link.tidal.com/AbCdE", "Short Tidal links"),
        ("https://tidal.com/playlist/not-a-uuid", "Tidal playlist"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "a single video"),
        ("https://youtu.be/dQw4w9WgXcQ", "a single video"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ", "mix"),
        ("https://www.youtube.com/playlist?list=WL", "private"),
        ("https://music.youtube.com/playlist?list=LM", "private"),
        ("https://www.youtube.com/@somechannel", "a channel"),
        ("https://music.youtube.com/browse/MPREb_abcdefghijk", "an album or artist page"),
        ("https://soundcloud.com/some-dj", "a SoundCloud profile"),
        ("https://soundcloud.com/some-dj/a-track", "a single track on SoundCloud"),
        ("https://on.soundcloud.com/AbCdEf", "Short SoundCloud links"),
        ("https://listenbrainz.org/user/someone/playlists/", "user page"),
        ("https://listenbrainz.org/playlist/not-an-id/", "ListenBrainz playlist"),
        ("https://www.beatport.com/track/think-of-you/30860950", "a single track on Beatport"),
        ("https://www.beatport.com/release/think-of-you-extended/7545183", "a release"),
        ("https://www.beatport.com/artist/prospa/460053", "an artist page"),
        ("https://www.beatport.com/label/steel-city-dance-discs/99436", "a label page"),
        ("https://www.beatport.com/genre/house/5", "a genre page"),
        ("https://www.beatport.com/genre/house/5/charts", "a list of Beatport charts"),
        ("https://www.beatport.com/charts", "a list of Beatport charts"),
        ("https://www.beatport.com/playlists/share/123456", "Shared Beatport playlists"),
        ("https://www.beatport.com/library/playlists/42", "Shared Beatport playlists"),
        ("https://www.beatport.com/", "Beatport chart"),
    ],
)
def test_other_links_say_why_not(link, reason):
    with pytest.raises(UnsupportedLink, match=reason):
        detect_source(link)


def test_youtube_music_is_not_taken_for_youtube():
    ref = detect_source(
        "https://music.youtube.com/playlist?list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG"
    )
    assert ref.source == "youtube_music"


def test_listenbrainz_created_for_you_series():
    ref = series_ref("dooziedan", "weekly-exploration")
    assert ref == PlaylistRef(
        source="listenbrainz",
        id="dooziedan/weekly-exploration",
        url="https://listenbrainz.org/user/dooziedan/",
        kind="series",
        label="ListenBrainz · Weekly Exploration for dooziedan",
    )
    assert set(SERIES) == {"weekly-exploration", "weekly-jams", "daily-jams"}
    assert series_title("dooziedan/daily-jams") == "Daily Jams (dooziedan)"
    with pytest.raises(UnsupportedLink, match="user name"):
        series_ref("", "weekly-jams")
    with pytest.raises(UnsupportedLink, match="Unknown series"):
        series_ref("dooziedan", "monthly-jams")
    with pytest.raises(UnsupportedLink):
        series_ref("a/b", "weekly-jams")


def test_sources_in_the_order_of_the_add_page():
    assert [s.label for s in SOURCES.values()] == [
        "ListenBrainz",
        "Apple Music",
        "Deezer",
        "Tidal",
        "YouTube Music",
        "YouTube",
        "SoundCloud",
        "Beatport",
    ]
    assert label("tidal") == "Tidal" and label("beatport") == "Beatport"
    assert label("spotify") == "spotify"  # unknown values stay as they are


@pytest.mark.parametrize("source", list(SOURCES))
def test_every_source_can_read_playlists(source):
    """Every source implements fetch() (none says "not ready" any more) and shows examples."""
    instance = get_source(source)
    assert isinstance(instance, Source)
    assert type(instance).fetch is not Source.fetch
    assert instance.examples
    for example in instance.examples:
        assert any(host in example for host in instance.hosts)
