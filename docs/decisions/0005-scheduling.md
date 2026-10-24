# 0005: Per-playlist schedules inside the app
Date: 2026-10-21 · Status: Accepted

## Context
Explo schedules runs with cron jobs that its start script writes from env vars, plus a second scheduler
in the web backend. Releasomat needs one schedule per playlist, editable in the UI.

## Decision
- The database is the source of truth: each playlist stores `schedule` (a cron expression, empty =
  manual only) and `next_refresh_at`. The UI offers presets (daily at a time, weekly on a day at a
  time) and a custom cron field.
- An in-process **APScheduler 3** `AsyncIOScheduler` holds one job per enabled playlist. Jobs are
  rebuilt from the database at startup and whenever a playlist's settings change. No cron daemon.
- A playlist never runs twice at the same time. A refresh that comes due while one is running is
  skipped and logged.
- If the container was off when a refresh was due, it runs once after startup (missed runs are
  combined into one).
- Downloads from all refreshes share one queue with a global limit (`DOWNLOAD_CONCURRENCY`, default 2).
- Times use `TZ`.

## Consequences
- One place to look at when a refresh did or did not run: the Activity page and the `run` table.
- If more scheduling features are needed later, APScheduler can be replaced behind `app/scheduler.py`.
