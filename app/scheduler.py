"""Per-playlist schedules (ADR 0005).

The database is the source of truth: each playlist stores ``schedule`` (a cron expression, empty
= only Refresh now) and ``next_refresh_at``. An APScheduler 3 ``AsyncIOScheduler`` holds one job
per enabled, scheduled playlist; ``PlaylistScheduler.rebuild()`` makes those jobs from the
database at startup, ``sync_playlist()`` after a playlist's settings changed.

If the container was off when a refresh was due (``next_refresh_at`` lies in the past at
startup), that playlist refreshes once right after startup: missed runs are combined into one.

The UI offers presets that map to cron expressions and back:

    off                      ""
    daily at 06:00           "0 6 * * *"
    weekly on Monday 06:00   "0 6 * * mon"
    custom                   any 5-field cron expression ("0 */6 * * *")

Weekdays in a custom expression count like standard cron (0 or 7 = Sunday, 1 = Monday). APScheduler
3 counts from Monday = 0, so ``make_trigger()`` turns weekday numbers into names first.
"""

import inspect
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.models import Playlist, Trigger

log = logging.getLogger(__name__)

PRESETS = {
    "off": "Off (only Refresh now)",
    "daily": "Daily",
    "weekly": "Weekly",
    "custom": "Custom (cron)",
}
WEEKDAYS = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}
# Standard cron numbering: 0 = Sunday, 1 = Monday, …, 6 = Saturday, 7 = Sunday again.
_CRON_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


class InvalidSchedule(ValueError):
    """A schedule that can't be used; the message says why (shown next to the field)."""


# --- Presets <-> cron expressions -----------------------------------------------------------


@dataclass
class Schedule:
    """A schedule as the playlist page's form shows it."""

    preset: str = "off"  # a key of PRESETS
    time: str = "06:00"  # HH:MM, for daily and weekly
    weekday: str = "mon"  # a key of WEEKDAYS, for weekly
    cron: str = ""  # for custom

    def to_cron(self) -> str:
        """The cron expression to store; raises InvalidSchedule."""
        if self.preset == "off":
            return ""
        if self.preset == "custom":
            expression = " ".join(self.cron.split())
            if not expression:
                raise InvalidSchedule("Enter a cron expression, e.g. 0 6 * * mon")
            make_trigger(expression, ZoneInfo("UTC"))  # raises InvalidSchedule
            return expression
        hour, minute = parse_time(self.time)
        if self.preset == "daily":
            return f"{minute} {hour} * * *"
        if self.preset == "weekly":
            if self.weekday not in WEEKDAYS:
                raise InvalidSchedule("Choose a weekday.")
            return f"{minute} {hour} * * {self.weekday}"
        raise InvalidSchedule(f"Unknown schedule {self.preset!r}.")

    @classmethod
    def from_cron(cls, expression: str) -> "Schedule":
        """The preset that gives this expression; "custom" when none does."""
        expression = " ".join((expression or "").split())
        if not expression:
            return cls()
        fields = expression.split()
        if len(fields) == 5 and fields[2] == fields[3] == "*":
            minute, hour, day = fields[0], fields[1], fields[4].lower()
            if minute.isdigit() and hour.isdigit() and int(minute) < 60 and int(hour) < 24:
                time = f"{int(hour):02d}:{int(minute):02d}"
                if day == "*":
                    return cls("daily", time)
                if day.isdigit() and int(day) <= 7:
                    day = _CRON_DAYS[int(day) % 7]
                if day in WEEKDAYS:
                    return cls("weekly", time, day)
        return cls("custom", cron=expression)

    def describe(self) -> str:
        """In words: "Daily at 06:00", "Every Monday at 06:00"."""
        if self.preset == "daily":
            return f"Daily at {self.time}"
        if self.preset == "weekly":
            return f"Every {WEEKDAYS[self.weekday]} at {self.time}"
        if self.preset == "custom":
            return f"Custom: {self.cron}"
        return "Manual only"


def describe(expression: str) -> str:
    return Schedule.from_cron(expression).describe()


def parse_time(text: str) -> tuple[int, int]:
    """'06:00' or '6:00' -> (6, 0); raises InvalidSchedule."""
    hour, _, minute = (text or "").strip().partition(":")
    if hour.isdigit() and minute.isdigit() and int(hour) < 24 and int(minute) < 60:
        return int(hour), int(minute)
    raise InvalidSchedule("Enter a time like 06:00.")


def _day_number(text: str) -> int:
    if text.isdigit() and int(text) <= 7:
        return int(text)
    if text in _CRON_DAYS:
        return _CRON_DAYS.index(text)
    raise InvalidSchedule(f"Unknown weekday {text!r} (use 0–7 or mon–sun).")


def day_of_week(field: str) -> str:
    """A standard cron weekday field as APScheduler 3 understands it, as names:
    "1-5" -> "mon,tue,wed,thu,fri", "0" and "7" -> "sun", "*/2" -> "sun,tue,thu,sat"."""
    field = field.lower()
    if field in ("*", "?"):
        return "*"
    days: set[int] = set()
    for part in field.split(","):
        span, _, step_text = part.partition("/")
        if step_text and not step_text.isdigit():
            raise InvalidSchedule(f"Bad step in {part!r}.")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise InvalidSchedule(f"Bad step in {part!r}.")
        if span == "*":
            start, end = 0, 6
        elif "-" in span:
            first, _, last = span.partition("-")
            start, end = _day_number(first), _day_number(last)
        else:
            start = _day_number(span)
            end = 6 if step_text else start  # "1/2": from Monday on, every second day
        if start > end:
            raise InvalidSchedule(f"Weekday range {span!r} goes backwards.")
        days.update(n % 7 for n in range(start, end + 1, step))
    names = [name for name in WEEKDAYS if _CRON_DAYS.index(name) in days]
    return "*" if len(names) == 7 else ",".join(names)


def make_trigger(expression: str, zone: ZoneInfo) -> CronTrigger:
    """An APScheduler trigger for a 5-field cron expression; raises InvalidSchedule."""
    fields = expression.split()
    if len(fields) != 5:
        raise InvalidSchedule(
            f"A cron expression has 5 fields (minute hour day month weekday), not {len(fields)}."
        )
    minute, hour, day, month, weekday = fields
    try:
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week(weekday),
            timezone=zone,
        )
    except ValueError as exc:
        if isinstance(exc, InvalidSchedule):
            raise
        raise InvalidSchedule(f"Invalid cron expression: {exc}") from None


def next_run(expression: str, zone: ZoneInfo, now: datetime | None = None) -> datetime | None:
    """When a schedule fires next (in ``zone``), or None for "off"; raises InvalidSchedule."""
    if not expression.strip():
        return None
    now = now or datetime.now(zone)
    return make_trigger(expression, zone).get_next_fire_time(None, now)


def to_utc(value: datetime | None) -> datetime | None:
    """How times are stored in the database: in UTC."""
    return value.astimezone(UTC) if value else None


# --- The running scheduler ------------------------------------------------------------------


class PlaylistScheduler:
    """One APScheduler job per enabled playlist with a schedule; ``on_due(playlist_id, trigger)``
    is called when one comes due (normally ``Worker.request_refresh``)."""

    def __init__(self, engine: Engine, on_due: Callable, zone: ZoneInfo):
        self.engine = engine
        self.on_due = on_due
        self.zone = zone
        self._scheduler = AsyncIOScheduler(
            timezone=zone,
            job_defaults={
                "coalesce": True,  # several missed runs (laptop asleep…) become one
                "max_instances": 1,
                "misfire_grace_time": None,  # late is better than never
            },
        )

    @staticmethod
    def job_id(playlist_id: int) -> str:
        return f"playlist-{playlist_id}"

    def start(self, now: datetime | None = None) -> list[int]:
        """Build the jobs from the database and start. Playlists whose refresh was due while
        Releasomat was off are refreshed once now; their IDs are returned."""
        missed = self.rebuild(now)
        self._scheduler.start()
        for playlist_id in missed:
            log.info(
                "Playlist %s missed its refresh while Releasomat was off: refreshing now",
                playlist_id,
            )
            self.on_due(playlist_id, Trigger.SCHEDULE.value)
        return missed

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def rebuild(self, now: datetime | None = None) -> list[int]:
        """Replace all jobs with ones made from the database. Returns the IDs of playlists whose
        stored next refresh lies before ``now`` (missed while Releasomat was off)."""
        now = now or datetime.now(self.zone)
        self._scheduler.remove_all_jobs()
        missed = []
        with Session(self.engine) as session:
            for playlist in session.exec(select(Playlist)).all():
                was_due = playlist.next_refresh_at
                upcoming = self._add_job(playlist, now)
                if upcoming and was_due and was_due <= to_utc(now):
                    missed.append(playlist.id)
                playlist.next_refresh_at = to_utc(upcoming)
            session.commit()
        return missed

    def sync_playlist(self, playlist_id: int, now: datetime | None = None) -> datetime | None:
        """After a playlist's settings changed (or it was added or removed): replace its job.
        Returns its next refresh time (None: no scheduled refreshes)."""
        now = now or datetime.now(self.zone)
        self._remove_job(playlist_id)
        with Session(self.engine) as session:
            playlist = session.get(Playlist, playlist_id)
            if playlist is None:
                return None
            upcoming = self._add_job(playlist, now)
            playlist.next_refresh_at = to_utc(upcoming)
            session.commit()
        return upcoming

    def jobs(self) -> dict[int, datetime | None]:
        """{playlist ID: next run time} of every job (for the tests and the logs)."""
        return {
            int(job.id.removeprefix("playlist-")): getattr(job, "next_run_time", None)
            for job in self._scheduler.get_jobs()
        }

    def _add_job(self, playlist: Playlist, now: datetime) -> datetime | None:
        if not playlist.enabled or not playlist.schedule:
            return None
        try:
            trigger = make_trigger(playlist.schedule, self.zone)
        except InvalidSchedule as exc:
            log.warning("Playlist %s has an invalid schedule: %s", playlist.id, exc)
            return None
        upcoming = trigger.get_next_fire_time(None, now)
        self._scheduler.add_job(
            self._due,
            trigger,
            args=[playlist.id],
            id=self.job_id(playlist.id),
            name=f"Refresh playlist {playlist.id}",
            replace_existing=True,
            next_run_time=upcoming,
        )
        return upcoming

    def _remove_job(self, playlist_id: int) -> None:
        with suppress(JobLookupError):
            self._scheduler.remove_job(self.job_id(playlist_id))

    async def _due(self, playlist_id: int) -> None:
        """A job fired: hand the refresh to the worker and store the next time."""
        result = self.on_due(playlist_id, Trigger.SCHEDULE.value)
        if inspect.isawaitable(result):
            await result
        job = self._scheduler.get_job(self.job_id(playlist_id))
        with Session(self.engine) as session:
            playlist = session.get(Playlist, playlist_id)
            if playlist is not None:
                playlist.next_refresh_at = to_utc(job.next_run_time if job else None)
                session.commit()
