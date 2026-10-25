# Architecture

Releasomat turns playlists from streaming services into playlists in Navidrome, using music files it
downloads itself. It runs as one Docker container on Unraid, next to Navidrome (and optionally slskd).

Design decisions are in [`decisions/`](decisions/). Research behind them: [`research/`](research/).
Phases and status: [`roadmap.md`](roadmap.md).

## What one refresh does

A **refresh** is one run for one playlist. It is started by the playlist's schedule or by **Refresh now**.

```
source (Apple Music, ListenBrainz, Deezer, …)
  │ 1. fetch: playlist name, cover, ordered tracks (artist, title, album, duration, ISRC/MBID if known)
  ▼
diff with the last refresh ──► tracks added / tracks removed
  │ 2. for every track without a usable song in Navidrome:
  ▼
Navidrome search ──found──► link the existing song (Releasomat does not download it)
  │ not found
  ▼
downloaders in the playlist's order (e.g. slskd first, then YouTube)
  │ 3. download → tag (artist, title, album, cover) → save under MUSIC_DIR
  ▼
Navidrome scan ──► 4. wait until finished, find the new songs by path
  ▼
Navidrome playlist ──► 5. create or update it in source order (missing tracks are left out)
  ▼
removed tracks ──► 6. leave the Navidrome playlist; their files stay (ADR 0003)
  ▼
run record + live events for the UI
```

A track that is in several playlists is downloaded once and shared.

Navidrome details (from `research/services.md` and the dev environment):
- **Finding a song Releasomat just downloaded**: the Subsonic `path` field is made up from tags by
  default, so it can't be used. Instead: write the recording MBID into the file when known and
  search for it (`search3` matches an MBID exactly); otherwise Navidrome's own API (`/api/song`
  filtered by path); fuzzy search by title + artist + duration only as the last resort.
- **Updating a playlist in place**: `createPlaylist` with `playlistId` replaces all songs and keeps
  the ID, so apps that sync playlists don't see a new one. Store the ID; never look playlists up
  by name. Send long song lists as a form POST.
- **Scans**: `startScan` needs an admin account and can target just Releasomat's folder. It answers
  "ok" even while a scan is running and then ignores the request: wait until `getScanStatus` says
  no scan is running, then trigger, then wait again.
- Subsonic requests carry the token and salt in the URL, and httpx logs request URLs at INFO. Keep
  httpx's logger at WARNING (or redact URLs) so secrets never reach the log.

After every download the file is **checked** (tags, duration) against the wanted track; on a
mismatch the next candidate is tried (a common complaint about Explo).

## Layout

```
app/
  main.py             FastAPI app: startup (migrations, scheduler, workers), routers, templates
  config.py           container settings from env vars (paths, PUID…) — pydantic-settings
  settings.py         user settings: DB values, env vars win (shown read-only in the UI)
  db.py, models.py    SQLite via SQLModel; migrations in app/migrations (Alembic)
  sources/            one module per service; all implement sources/base.py
    base.py           Source interface, PlaylistSnapshot (+ warnings), SourceTrack (+ BPM, key),
                      URL detection
    listenbrainz.py   playlists + "Created for you" series (newest by date), retries when LB is slow
    apple_music.py    public playlist page (JSON inside the HTML), no ISRC
    deezer.py         public API, paged, ISRC
    tidal.py          official API v2, needs TIDAL_CLIENT_ID/SECRET
    youtube.py        YouTube Music (ytmusicapi) and YouTube (yt-dlp, flat list)
    soundcloud.py     yt-dlp, one read per track (slow for big sets)
    beatport.py       charts, Top 100 / Hype pages (JSON inside the HTML); share links not readable
    web.py            shared: error building, secret scrubbing, retries
    ytdlp.py, titles.py  shared: yt-dlp calls, "Artist - Title" splitting
  downloaders/        one module per downloader; all implement downloaders/base.py
    base.py           Downloader interface, DownloadResult
    youtube.py        starts one worker process per download (ADR 0007)
    youtube_job.py    the worker: ytmusicapi search + yt-dlp download as libraries, JSON in/out
    slskd.py          Soulseek through the slskd REST API
  navidrome.py        the only place that talks to Navidrome (Subsonic API)
  matching.py         normalising and scoring "is this the same track?"
  tagging.py          writes tags and cover art with mutagen
  files.py            the only place that creates, moves or deletes music files (ADR 0003)
  lifecycle.py        what happens to a file after the download: adopted by Tagwerk, "Don't
                      like", left the folder, found in the library (ADR 0009; database only)
  pipeline.py         one refresh, steps 1–6 above
  scheduler.py        per-playlist schedules (APScheduler), rebuilt from the DB (ADR 0005)
  worker.py           queue of refreshes and downloads, concurrency limits
  events.py           in-process event bus → Server-Sent Events for live progress
  routes/             pages (HTML, htmx partials) and JSON API, same routes as /docs
    integration.py    the optional integration API for Tagwerk (ADR 0009), off without a key
  templates/          Jinja2 (base layout, pages, partials/)
  static/             vendored htmx (+ SSE extension), Pico CSS, theme.css (colour tokens), icons
tests/                pytest; never go online (saved answers in tests/fixtures/online/)
docker/entrypoint.sh  PUID/PGID/UMASK, then uvicorn
unraid/releasomat.xml Unraid template
```

## Data model (first version)

| Table | Holds |
|---|---|
| `playlist` | source type, `kind` (a fixed playlist or a ListenBrainz series) + URL/ID, title, cover, `enabled` (pauses the schedule; Refresh now still works), `schedule` (cron string or empty), Navidrome mode, Navidrome playlist ID, last/next refresh, last status |
| `track` | one real-world track: artist, title, album, duration, ISRC, MusicBrainz recording ID, cover URL, BPM and key (when the source knows them), `match_key` (normalised artist + title, for finding the same track again). Shared between playlists |
| `playlist_track` | which tracks a playlist has, position, `added_at`, `removed_at` |
| `track_file` | where the track lives: Navidrome song ID, file path under `MUSIC_DIR` (empty if the song was already in the library), `downloaded_by_releasomat`, downloader, format/bitrate, status (`pending`, `searching`, `downloading`, `done`, `in_library`, `failed`, `skipped`, `adopted`, `disliked`), error, `details` (JSON: candidates tried and why they failed), attempts; for files that left the folder (ADR 0009, `app/lifecycle.py`): `former_path`, `adopted_at`, `disliked_at`, `disliked_reason` |
| `run` | one refresh: playlist, trigger (`schedule`/`manual`), start/end, status (incl. `skipped` when a scheduled refresh came due while one was running, `stopped` when the owner
pressed Stop), `message`, counts, log lines, `report` (JSON error report) |
| `navidrome_snapshot` | the Navidrome IDs of snapshot playlists Releasomat created, per playlist (only these may ever be deleted, and only with `SNAPSHOT_AUTO_DELETE` on) |
| `setting` | key/value user settings (connections, defaults) |

## Files after the download (ADR 0009)

Releasomat's folder is a *discovery area*: its playlists work right away, and the owner's curated
library stays separate. Files leave the folder only by the owner's action (ADR 0003). Every change
of a file's state goes through `app/lifecycle.py` (database only, except the owner's delete); the
refresh, the integration API and the UI all call it.

| State (`track_file.status`) | Meaning | Downloaded again? |
|---|---|---|
| `done` | downloaded, in Releasomat's folder | – |
| `in_library` | linked to a song elsewhere in the library (also an adopted or moved file, once Navidrome lists it there) | no |
| `adopted` ("Adopted by Tagwerk") | Tagwerk took the file into its inbox; linked once it is in the library | never |
| `disliked` ("Don't like") | the owner deleted it (in Releasomat, with Tagwerk's Delete, or threw the adopted file away in Tagwerk's inbox), or its file left the folder and isn't anywhere else in the library | never, until **Undo** on the playlist page |

The status says where the song is now (the badge); `adopted_at`, `disliked_at`, `disliked_reason`
(`vanished`, `deleted`, `discarded`, `owner`) and `former_path` remember what happened, so a "Don't
like" track can also be `in_library` when the song is elsewhere in the library.

**Without Tagwerk** (the default) all of this works through Navidrome alone: each refresh checks
the files it downloaded. A file that has left the folder without an adoption notice is looked for
in Navidrome *outside* Releasomat's folder (MusicBrainz ID, ISRC, name and length; the real path
decides): found means linked there ("moved into your library"), not found means "Don't like"
(lifted by itself if the song turns up later). **Safety brake**: when the folder looks unmounted
(missing, empty, or every expected file of a playlist gone at once, at least 5), nothing is marked
and the run warns.

## Integration API for Tagwerk (optional, ADR 0009)

**Releasomat never depends on Tagwerk.** The integration API under `/api/integration/` is off until
the owner sets `INTEGRATION_API_KEY` (Settings → Tagwerk; every call answers 403
`integration_off`). Releasomat never calls Tagwerk; only Tagwerk calls Releasomat, so nothing in a
refresh waits for it.

- Key in `Authorization: Bearer <key>` (constant-time compare, checked before the body; missing or
  wrong: 401). Never logged, never echoed, scrubbed from error reports.
- `GET info` (app, version, `integration_api: 1`, the folder name), `GET files` (the files in the
  area now, with what was wanted and which playlists contain them).
- `POST adopt`, `returned`, `discarded`, `restored`, `delete`: `{"files": [{"path", "size",
  "id"}]}`, one answer per file (`ok` or a plain `reason`), idempotent. Paths inside the area in
  NFC (`Releasomat/…` and `/music/Releasomat/…` accepted; `..` refused).
- Only `delete` touches a file, through Releasomat's own owner's delete
  (`lifecycle.delete_and_dislike` → `files.delete_by_owner`); every other call changes only the
  database.
- Errors: `{"integration_api": 1, "error": "<code>", "message": "…"}`. Shapes in `/docs`.
- The Settings page shows whether it is on, the address and folder to enter in Tagwerk, and
  Tagwerk's last call (kept in memory since the start).

## Error reports

When a refresh fails (a source can't be read, Navidrome doesn't answer, …) or single tracks fail
(no download found, wrong file), the run keeps an **error report** (ADR 0006):

- **What and where**, in plain language: "Apple Music: the playlist page has changed, Releasomat
  can't find the track list", "Navidrome didn't answer within 10 s".
- **Details for fixing it**: step (read source / match / download / scan / playlist), source and
  link, error type, HTTP status, a short excerpt of the answer, Releasomat and yt-dlp versions, time.
  Never passwords, tokens, API keys or cookies.
- Per track that failed: which track, which downloaders were tried, why each one failed.
- Shown on the playlist page and the Activity page, with **Copy report** (plain text, ready to paste
  into a GitHub issue or a chat).

A failed read changes nothing in Navidrome: the playlist keeps its last good state.

## Download sources (global, ADR 0008)

One order for all playlists, set in Settings → "Download sources": every source with an on/off
switch, moved up and down (default: Soulseek, then YouTube). For each missing track Releasomat tries
the enabled sources in this order until one delivers a checked file. Soulseek also has to meet the
quality rules (`SLSKD_FORMATS`, `SLSKD_MIN_BITRATE`); otherwise the next source is tried. A source
that isn't connected is skipped with one note per run. Nothing enabled (`DOWNLOAD_SOURCES=none`):
only songs already in Navidrome are linked. Changes on the page apply after "Save settings".

## Per-playlist settings

- **Schedule**: off, daily at a time, weekly on a day at a time, or a custom cron expression.
- **Navidrome mode**: `update` keeps one Navidrome playlist in sync (same ID, only the songs change);
  `snapshot` creates a new, frozen Navidrome playlist per refresh, named with the date.
  Old snapshot playlists stay until the owner deletes them, unless the global setting
  `SNAPSHOT_AUTO_DELETE` is on: then Releasomat keeps the newest N per playlist (N set per playlist)
  and deletes older ones. It only ever deletes Navidrome *playlists* it created itself, found by
  their stored ID, never by name, and never files.
- There is **no** setting for deleting files: Releasomat never deletes music on its own. When a track
  leaves a playlist, its file stays; the owner deletes files by hand (ADR 0003).
- **ListenBrainz "Created for you"** playlists are a *series* (Weekly Exploration, Weekly Jams, Daily
  Jams…): each refresh takes the newest playlist of that series.

## Configuration

Container settings (env vars only, see ADR 0002):

| Variable | Default | Meaning |
|---|---|---|
| `CONFIG_DIR` | `/config` | database, `cookies.txt` for yt-dlp, cache |
| `MUSIC_DIR` | `/music` | Releasomat's own folder **inside** the Navidrome library; the only place it writes music |
| `SLSKD_DOWNLOADS_DIR` | `/slskd` | slskd's download folder (optional) |
| `PUID` / `PGID` / `UMASK` | `99` / `100` / `022` | Unraid file ownership |
| `TZ` | `UTC` | time zone for schedules |
| `LOG_LEVEL` | `INFO` | |

User settings (editable in the UI; an env var with the same name wins and locks the field):
`NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`, `NAVIDROME_SUBFOLDER` (name of Releasomat's
folder as Navidrome sees it, e.g. `Releasomat`), `SLSKD_URL`, `SLSKD_API_KEY`, `LISTENBRAINZ_USER`,
`LISTENBRAINZ_TOKEN`, `TIDAL_CLIENT_ID`, `TIDAL_CLIENT_SECRET`, `DOWNLOAD_CONCURRENCY` (default 2),
`MATCH_THRESHOLD`, `PATH_TEMPLATE` (default `{artist}/{artist} - {title}.{ext}`),
`SNAPSHOT_AUTO_DELETE` (default off), `DOWNLOAD_SOURCES` (global order of download sources, default
`slskd,youtube`; ADR 0008), `SLSKD_FORMATS` (default `flac,aiff,wav,mp3`), `SLSKD_MIN_BITRATE`
(default 320), `INTEGRATION_API_KEY` (optional, empty by default: the Tagwerk integration is off;
at least 16 characters).

Secrets are never logged and never sent back to the browser in full.

## Web UI

Server-rendered pages with htmx (ADR 0001). Port **8430**.

- **Playlists** (`/`): cover, name, source, tracks in library / downloaded / missing / failed (plus
  adopted / Don't like when there are some), schedule, next refresh, last status, **Refresh now**.
- **Add playlist** (`/playlists/new`): paste a URL (source is detected), or pick from the ListenBrainz
  account (own playlists and "Created for you").
- **Playlist** (`/playlists/{id}`): tracks with status ("Adopted by Tagwerk" and "Don't like"
  badges, where each song comes from, **Undo** for Don't like: `DELETE /api/tracks/{id}/dislike`);
  settings (above).
- **Activity** (`/activity`): runs and live download progress (Server-Sent Events).
- **Stop** (playlist page, Activity; `POST /api/playlists/{id}/stop`, `POST /api/runs/{id}/stop`):
  ends a running refresh now (the YouTube worker process group is ended, a Soulseek search or
  transfer cancelled) or drops a queued one. Saved tracks stay; the Navidrome playlist is not
  changed. Once the playlist is being saved in Navidrome it is too late to stop.
- **Settings** (`/settings`): connections with a **Test** button, defaults, and the optional group
  "Tagwerk" (key with **Generate**, saved only with "Save settings"; a status box). A short setup
  wizard on first start until Navidrome is connected (planned; it never asks for the Tagwerk key).
- `/docs` (API), `/health`.
