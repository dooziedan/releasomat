"""Schedules: presets <-> cron, next-run times, and the jobs rebuilt from the database (ADR 0005).
Nothing here waits for a real schedule to fire."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, select

from app.models import Playlist
from app.scheduler import (
    InvalidSchedule,
    PlaylistScheduler,
    Schedule,
    day_of_week,
    describe,
    next_run,
)

BERLIN = ZoneInfo("Europe/Berlin")
UTC_ZONE = ZoneInfo("UTC")


@pytest.mark.parametrize(
    ("schedule", "cron"),
    [
        (Schedule("off"), ""),
        (Schedule("daily", "06:00"), "0 6 * * *"),
        (Schedule("daily", "23:59"), "59 23 * * *"),
        (Schedule("weekly", "06:30", "mon"), "30 6 * * mon"),
        (Schedule("weekly", "00:05", "sun"), "5 0 * * sun"),
        (Schedule("custom", cron="0 */6 * * *"), "0 */6 * * *"),
        (Schedule("custom", cron="15 4 1 * *"), "15 4 1 * *"),
    ],
)
def test_presets_round_trip(schedule, cron):
    assert schedule.to_cron() == cron
    assert Schedule.from_cron(cron) == schedule


def test_cron_written_by_hand_becomes_a_preset_where_possible():
    assert Schedule.from_cron("  0  6 * * * ") == Schedule("daily", "06:00")
    assert Schedule.from_cron("0 6 * * 1") == Schedule("weekly", "06:00", "mon")
    assert Schedule.from_cron("0 6 * * 0") == Schedule("weekly", "06:00", "sun")
    assert Schedule.from_cron("0 6 * * 7") == Schedule("weekly", "06:00", "sun")
    assert Schedule.from_cron("0 6 * * 1-5").preset == "custom"
    assert Schedule.from_cron("0 6 1 * *").preset == "custom"


def test_schedules_in_words():
    assert describe("") == "Manual only"
    assert describe("0 6 * * *") == "Daily at 06:00"
    assert describe("30 7 * * fri") == "Every Friday at 07:30"
    assert describe("0 */6 * * *") == "Custom: 0 */6 * * *"


@pytest.mark.parametrize(
    "schedule",
    [
        Schedule("daily", "25:00"),
        Schedule("daily", "six"),
        Schedule("weekly", "06:00", "someday"),
        Schedule("custom", cron=""),
        Schedule("custom", cron="0 6 * *"),
        Schedule("custom", cron="61 6 * * *"),
        Schedule("custom", cron="0 6 * * 8"),
        Schedule("custom", cron="0 6 * * fri-mon"),
        Schedule("bogus"),
    ],
)
def test_invalid_schedules_are_refused(schedule):
    with pytest.raises(InvalidSchedule):
        schedule.to_cron()


def test_weekdays_count_like_standard_cron():
    """APScheduler 3 counts Monday as 0; cron users expect Sunday = 0 (and 7)."""
    assert day_of_week("*") == "*"
    assert day_of_week("1") == "mon"
    assert day_of_week("0") == day_of_week("7") == "sun"
    assert day_of_week("1-5") == "mon,tue,wed,thu,fri"
    assert day_of_week("0,6") == "sat,sun"
    assert day_of_week("*/2") == "tue,thu,sat,sun"  # 0, 2, 4, 6
    assert day_of_week("MON-wed") == "mon,tue,wed"
    assert day_of_week("0-7") == "*"


def test_next_run_in_the_time_zone():
    monday_noon = datetime(2026, 10, 19, 12, 0, tzinfo=BERLIN)  # a Monday
    assert next_run("0 6 * * *", BERLIN, monday_noon) == datetime(2026, 10, 20, 6, 0, tzinfo=BERLIN)
    assert next_run("0 6 * * mon", BERLIN, monday_noon) == datetime(
        2026, 10, 26, 6, 0, tzinfo=BERLIN
    )
    # Standard cron weekday 1 is Monday here too, not Tuesday.
    assert next_run("0 6 * * 1", BERLIN, monday_noon).weekday() == 0
    # 06:00 in Berlin is 04:00 UTC in summer time and 05:00 UTC after it ends (Oct 25).
    assert next_run("0 6 * * *", BERLIN, monday_noon).astimezone(UTC).hour == 4
    after_change = datetime(2026, 10, 26, 12, 0, tzinfo=BERLIN)
    assert next_run("0 6 * * *", BERLIN, after_change).astimezone(UTC).hour == 5
    assert next_run("", BERLIN, monday_noon) is None


def _playlist(session, source_id, **values) -> int:
    playlist = Playlist(source="deezer", source_id=source_id, url="https://x", title=source_id)
    for key, value in values.items():
        setattr(playlist, key, value)
    session.add(playlist)
    session.commit()
    return playlist.id


def test_jobs_are_rebuilt_from_the_database(engine):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC_ZONE)  # a Wednesday
    with Session(engine) as session:
        missed = _playlist(
            session, "missed", schedule="0 6 * * *", next_refresh_at=now - timedelta(hours=6)
        )
        weekly = _playlist(session, "weekly", schedule="0 6 * * mon")
        paused = _playlist(
            session, "paused", schedule="0 6 * * *", enabled=False, next_refresh_at=now
        )
        manual = _playlist(session, "manual", schedule="")
        broken = _playlist(session, "broken", schedule="not a cron")

    calls = []
    scheduler = PlaylistScheduler(engine, lambda *args: calls.append(args), UTC_ZONE)
    assert scheduler.rebuild(now) == [missed]  # the container was off at 06:00
    assert calls == []  # rebuild() only reports; start() refreshes
    assert scheduler.jobs() == {
        missed: datetime(2026, 10, 22, 6, 0, tzinfo=UTC_ZONE),
        weekly: datetime(2026, 10, 26, 6, 0, tzinfo=UTC_ZONE),
    }
    with Session(engine) as session:
        stored = {p.id: p.next_refresh_at for p in session.exec(select(Playlist))}
    assert stored[missed] == datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
    assert stored[weekly] == datetime(2026, 10, 26, 6, 0, tzinfo=UTC)
    assert stored[paused] is stored[manual] is stored[broken] is None

    # Rebuilding again changes nothing and finds nothing missed (the times moved on).
    assert scheduler.rebuild(now) == []
    assert len(scheduler.jobs()) == 2


def test_changed_settings_replace_the_job(engine):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC_ZONE)
    with Session(engine) as session:
        playlist_id = _playlist(session, "p", schedule="0 6 * * *")
    scheduler = PlaylistScheduler(engine, lambda *args: None, UTC_ZONE)
    scheduler.rebuild(now)

    with Session(engine) as session:
        session.get(Playlist, playlist_id).schedule = "30 18 * * *"
        session.commit()
    assert scheduler.sync_playlist(playlist_id, now) == datetime(
        2026, 10, 21, 18, 30, tzinfo=UTC_ZONE
    )
    assert scheduler.jobs() == {playlist_id: datetime(2026, 10, 21, 18, 30, tzinfo=UTC_ZONE)}

    with Session(engine) as session:
        session.get(Playlist, playlist_id).enabled = False
        session.commit()
    assert scheduler.sync_playlist(playlist_id, now) is None
    assert scheduler.jobs() == {}


async def test_start_refreshes_missed_playlists_once(engine):
    zone = UTC_ZONE
    now = datetime.now(zone)
    with Session(engine) as session:
        missed = _playlist(
            session, "missed", schedule="0 6 * * *", next_refresh_at=now - timedelta(days=3)
        )
        _playlist(session, "fine", schedule="0 6 * * *", next_refresh_at=now + timedelta(hours=1))
    calls = []
    scheduler = PlaylistScheduler(engine, lambda *args: calls.append(args), zone)
    try:
        assert scheduler.start() == [missed]
        assert calls == [(missed, "schedule")]  # three missed days become one refresh
    finally:
        scheduler.shutdown()


async def test_a_due_job_hands_the_refresh_to_the_worker(engine):
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC_ZONE)
    with Session(engine) as session:
        playlist_id = _playlist(session, "p", schedule="0 6 * * *")
    calls = []

    async def on_due(*args):
        calls.append(args)

    scheduler = PlaylistScheduler(engine, on_due, UTC_ZONE)
    scheduler.rebuild(now)
    await scheduler._due(playlist_id)
    assert calls == [(playlist_id, "schedule")]
    with Session(engine) as session:
        assert session.get(Playlist, playlist_id).next_refresh_at == datetime(
            2026, 10, 22, 6, 0, tzinfo=UTC
        )
