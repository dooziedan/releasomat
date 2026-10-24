"""Error reports: plain message, details for fixing, and never a secret."""

from datetime import UTC, datetime

from app import reports
from app import settings as user_settings
from app.navidrome import NavidromeError
from app.reports import as_text, cookie_values, failure, for_tracks, known_secrets, scrub
from app.sources.base import SourceError

NOW = datetime(2026, 10, 22, 18, 30, tzinfo=UTC)
COOKIES = """# Netscape HTTP Cookie File
.youtube.com\tTRUE\t/\tTRUE\t1790000000\tSAPISID\tsapisid-value-1234
#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1790000000\t__Secure-3PSID\tpsid-secret-5678
.youtube.com\tTRUE\t/\tTRUE\t1790000000\tPREF\tf6=8
"""


def test_scrub_removes_every_kind_of_secret():
    secrets = ["sesame-password", "lb-token-123456", "tidal-secret-abc"]
    text = (
        "GET http://nd:4533/rest/ping?u=admin&t=26719a1196d2a940705a59634eb18eab&s=c19b2d&v=1 "
        "password sesame-password, ListenBrainz lb-token-123456, Tidal tidal-secret-abc "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig "
        "X-ND-Authorization: Bearer abc.def.ghi "
        "Cookie: SAPISID=sapisid-value-1234; PREF=f6=8 "
        '{"subsonicToken": "0123456789abcdef", "token": "jwt.jwt.jwt", "client_secret": "x1"} '
        "https://api.example/v1?apikey=SLSKD-KEY-99&page=2"
    )
    clean = scrub(text, secrets)
    for secret in (
        "26719a1196d2a940705a59634eb18eab",
        "c19b2d",
        "sesame-password",
        "lb-token-123456",
        "tidal-secret-abc",
        "eyJhbGciOiJIUzI1NiJ9",
        "abc.def.ghi",
        "sapisid-value-1234",
        "0123456789abcdef",
        "jwt.jwt.jwt",
        "SLSKD-KEY-99",
        '"x1"',
    ):
        assert secret not in clean, secret
    assert "u=admin" in clean and "page=2" in clean  # the harmless parts stay readable
    nested = scrub({"a": ["key sesame-password"], "b": 3}, secrets)
    assert nested == {"a": ["key [hidden]"], "b": 3}


def test_secrets_come_from_the_settings_and_cookies_txt(session, tmp_path, monkeypatch):
    (tmp_path / "cookies.txt").write_text(COOKIES)
    assert cookie_values(tmp_path / "cookies.txt") == ["sapisid-value-1234", "psid-secret-5678"]
    user_settings.save(session, "LISTENBRAINZ_TOKEN", "lb-token-123456")
    monkeypatch.setenv("NAVIDROME_PASSWORD", "sesame-password")
    monkeypatch.setenv("TIDAL_CLIENT_SECRET", "tidal-secret-abc")
    monkeypatch.setenv("SLSKD_API_KEY", "slskd-key-0000")
    found = known_secrets(session, tmp_path)
    for value in (
        "lb-token-123456",
        "sesame-password",
        "tidal-secret-abc",
        "slskd-key-0000",
        "sapisid-value-1234",
        "psid-secret-5678",
    ):
        assert value in found


def test_a_failed_read_report_has_the_source_details():
    error = SourceError(
        "the playlist page has changed, Releasomat can't find the track list",
        url="https://music.apple.com/de/playlist/pl.123",
        http_status=200,
        excerpt="<html>   <body>new layout?token=abc123</body>",
        step="read source",
    )
    report = failure("read", error, prefix="Apple Music", source="Apple Music", now=NOW)
    assert report["message"] == (
        "Apple Music: the playlist page has changed, Releasomat can't find the track list"
    )
    assert report["link"] == "https://music.apple.com/de/playlist/pl.123"
    assert (report["http_status"], report["error_type"]) == (200, "SourceError")
    assert report["excerpt"] == "<html> <body>new layout?token=[hidden]</body>"
    assert report["details"] == {"step": "read source"}
    assert report["versions"]["releasomat"] and report["versions"]["yt_dlp"]
    assert report["time"] == "2026-10-22T18:30:00+00:00"


def test_unexpected_errors_say_where_they_happened():
    try:
        {}["missing"]
    except KeyError as exc:
        report = failure("match", exc, prefix="Navidrome", secrets=["sesame"])
    assert report["message"] == "Unexpected error: KeyError: 'missing'"
    assert "test_reports.py" in report["details"]["where"]


def test_navidrome_errors_never_carry_the_login():
    error = NavidromeError("No answer from http://nd:4533 in time.", call="getScanStatus")
    report = failure("scan", error, prefix="Navidrome", secrets=["sesame"])
    assert report["message"] == "Navidrome: No answer from http://nd:4533 in time."
    assert report["details"] == {"call": "getScanStatus"}


def test_copy_report_text():
    failed = [
        {
            "position": 3,
            "artist": "Daft Punk",
            "title": "Get Lucky",
            "message": "YouTube: 2 download(s) tried, none worked",
            "tried": [
                {"downloader": "youtube", "candidate": "YouTube Music: x", "reason": "too long"},
                {"downloader": "slskd", "candidate": "", "reason": "Soulseek isn't set up."},
            ],
        }
    ]
    report = for_tracks(failed, source="Deezer", link="https://deezer.com/playlist/1", now=NOW)
    text = as_text(report, "My Mix")
    assert text.startswith("Releasomat error report\n\nPlaylist: My Mix\n")
    assert "What happened: 1 track couldn't be downloaded." in text
    assert "Track 3: Daft Punk – Get Lucky" in text
    assert "  - youtube: YouTube Music: x: too long" in text
    assert "  - slskd: : Soulseek isn't set up." in text
    assert f"yt-dlp {reports.versions()['yt_dlp']}" in text
    assert as_text(None) == ""
