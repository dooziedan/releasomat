# Development

You develop Releasomat against a **test Navidrome** with a small **demo library**, never against
your real music or your real Navidrome. Everything for that lives in the `dev/` folder, which git
ignores, so nothing in it can end up in the repository.

## What you need

- **Docker** with `docker compose` (runs the test Navidrome and, if you want, the Releasomat
  container).
- **Python 3.13 or newer** (`python3 --version`).
- **ffmpeg** (only to make the demo library; inside the Releasomat image it is already included).
- **curl** (used by `scripts/dev_up.sh`).

## First-time setup

```sh
python3 -m venv .venv                  # a private Python just for this project
.venv/bin/pip install -e '.[dev]'      # Releasomat and the developer tools
scripts/dev_up.sh                      # test Navidrome, demo library, dev/.env
```

## `scripts/dev_up.sh`

Run it once at the start, and again whenever you like (for example after restarting the computer,
because the test Navidrome doesn't start by itself). It only adds what is missing:

1. creates the `dev/` folders,
2. creates `dev/.env` with new random test passwords (only if it doesn't exist),
3. creates the demo library in `dev/library/Demo`,
4. starts the test Navidrome on http://localhost:4533 and creates its admin user,
5. lets Navidrome scan the library and prints how many songs it found (22).

The demo library has 22 sounds (beeps or noise, as long as the real songs) with the tags of well-known
songs, so playlists from Apple Music, Deezer or ListenBrainz find some of them "already in
Navidrome". A few are deliberately tricky for matching: "feat." in the title and in the artist,
"- Remastered 2011", accents and umlauts, "and" instead of "&", the same title by two artists
(Halo, Hurt) and a remix. The list is in `scripts/make_demo_library.py`.

### What is in `dev/`

| Path | What it is |
|---|---|
| `dev/.env` | Settings for running Releasomat locally, including the test Navidrome's login |
| `dev/library/` | The test Navidrome's music folder |
| `dev/library/Demo/` | The demo library |
| `dev/library/Releasomat/` | Releasomat's own music folder (`MUSIC_DIR`); Navidrome sees it as `Releasomat` |
| `dev/config/` | Releasomat's database and `cookies.txt` when it runs locally (`CONFIG_DIR`) |
| `dev/config-docker/` | The same for the Releasomat container (a separate database) |
| `dev/slskd/` | `SLSKD_DOWNLOADS_DIR`: where the fake slskd puts its "downloads" |
| `dev/navidrome-data/` | The test Navidrome's database |
| `dev/screenshots/` | Output of `scripts/screenshots.py` |

### Logins

The passwords are random test values made by `dev_up.sh`. They are only for these test services
and only stored in `dev/` (readable only by you). Never copy them into tracked files.

- **Test Navidrome** (http://localhost:4533): user `NAVIDROME_USER` (`admin`), password
  `NAVIDROME_PASSWORD`, both in `dev/.env`. Show the password with
  `grep NAVIDROME_PASSWORD dev/.env`.

The test Navidrome only scans when someone asks it to (Releasomat, the scan button in its web page,
or `dev_up.sh`). It has no scan schedule and doesn't watch the folders, so you notice when
Releasomat forgets to start a scan.

Navidrome's Subsonic API shows made-up file paths (`Artist/Album/01 - Title.mp3`) unless the client
has "Report Real Path" switched on in Navidrome's player settings. The test Navidrome keeps
Navidrome's default, like a normal installation.

## Run Releasomat locally

```sh
.venv/bin/uvicorn app.main:app --reload --port 8430 --env-file dev/.env
```

Open http://localhost:8430 (the API is at http://localhost:8430/docs). `--reload` restarts the app
whenever you save a file. Releasomat uses `dev/config` for its database and saves music to
`dev/library/Releasomat`, which the test Navidrome scans.

**Settings that are set in `dev/.env` are locked** on the Settings page: an env var always wins and
is shown read-only. To try a setting in the UI, or to test the setup wizard of a fresh install,
put a `#` in front of the lines in `dev/.env` (for example all `NAVIDROME_…` lines) and restart.
`dev_up.sh` still finds the commented-out Navidrome login. For a completely fresh start also delete
the database: `scripts/dev_down.sh --reset`.

## Run the container against the test Navidrome

This builds the image and runs it the way it runs on Unraid, on port **8431** (so it can run next
to the local one):

```sh
docker compose -f docker-compose.dev.yml --profile app up --build releasomat
```

Open http://localhost:8431. Stop it with Ctrl+C. The container reads the settings from `dev/.env`,
but uses its own paths (`/config` = `dev/config-docker`, `/music` = `dev/library/Releasomat`,
`/slskd` = `dev/slskd`) and reaches Navidrome by its Docker name, `http://navidrome:4533`, never
`localhost`. It keeps its own database in `dev/config-docker`, separate from the local run's.

The plain `docker compose up --build` from `CLAUDE.md` uses `docker-compose.yml` instead
(port 8430), not this test setup.

## Tests and lint

```sh
.venv/bin/pytest                    # tests (never go online)
.venv/bin/ruff check .              # lint
.venv/bin/ruff format .             # format
```

CI runs the same checks on every push and pull request.

## Database changes (migrations)

Releasomat updates its database itself when it starts. After changing `app/models.py`, create a
migration and commit it:

```sh
CONFIG_DIR=dev/config .venv/bin/alembic upgrade head        # bring your dev database up to date
CONFIG_DIR=dev/config .venv/bin/alembic revision --autogenerate -m "add rating column"
```

Check the new file in `app/migrations/versions/`. A test fails if the models and migrations drift
apart. Never edit a migration that was already released, and give new NOT NULL columns a
`server_default`.

## Screenshots in all browsers

Before a pull request that changes the interface, start Releasomat and run:

```sh
.venv/bin/python scripts/screenshots.py                          # http://localhost:8430
.venv/bin/python scripts/screenshots.py http://localhost:8431    # the container
```

It saves every main page in Chromium, Firefox and WebKit (Safari's engine), at desktop and phone
width, light and dark, to `dev/screenshots/<date and time>/`. It fails if a page has a JavaScript
error or anything answers with an error.

This needs Playwright's browsers, which live in `~/.cache/ms-playwright`. When this guide was
written they were **not** installed on the development PC yet (that folder didn't exist). Install
them once with `.venv/bin/playwright install chromium firefox webkit` (a few hundred MB). WebKit
may be missing system libraries on Arch/CachyOS; then use Playwright's Docker image, which has
everything (it is already downloaded on the development PC):

```sh
docker run --rm --network host -v "$PWD:/work" -w /work \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    mcr.microsoft.com/playwright/python:v1.63.0-noble \
    sh -c "pip install -q --user playwright==1.63.0 && python scripts/screenshots.py"
```

## Soulseek

Releasomat talks to slskd (a separate program that connects to Soulseek) through slskd's web API
(`app/downloaders/slskd.py`). In development it talks to a **fake slskd** instead
(`scripts/fake_slskd.py`, ADR 0008): it answers like slskd 0.26.1, but never touches the Soulseek
network, needs no account and no slskd image. Its "peers" share the songs of the demo library plus
a few made-up ones; a download takes a few seconds and then a quiet tone as long as the song (made
with ffmpeg) appears in `dev/slskd/`, where slskd would put it.

Start it (or use the `fake-slskd` entry in `.claude/launch.json`):

```sh
.venv/bin/python scripts/fake_slskd.py            # http://localhost:5030, files into dev/slskd
.venv/bin/python scripts/fake_slskd.py --speed 5  # five times faster
.venv/bin/python scripts/fake_slskd.py --scenario stall   # every search plays one scenario
```

Then point the local app at it: **Settings → Soulseek**, address `http://localhost:5030`, API key
`releasomat-fake-slskd-key` (the fake's fixed dev key; `--api-key` or `SLSKD_FAKE_API_KEY` set
another). The Test button should say "Connected to slskd 0.26.1, logged in to Soulseek."
`--logged-out` makes the fake act as if slskd weren't logged in. For the Releasomat container
(port 8431) start the fake with `--host 0.0.0.0` and use `http://host.docker.internal:5030` or the
computer's IP address instead of `localhost`.

**Scenarios** play through what goes wrong on Soulseek. `--scenario NAME` applies one to every
search; without it, a scenario word in the search text picks it, and the fake shares a song for each
word ("Releasomat Test – stall", "Releasomat Test – wrongfile", …):

| Scenario | What the fake does | What Releasomat should do |
|---|---|---|
| `normal` | FLAC 16 bit, MP3 320, MP3 128, a busy 24-bit peer, a live version, an M4A, a locked file | take the FLAC |
| `noresults` | nobody answers | search again more simply, then give up: "Nobody shares this track" |
| `lowquality` | only MP3 128, MP3 VBR ~190, M4A | give up, listing each file and why ("128 kbit/s, at least 320 wanted") |
| `offline` | the best peer is offline (404) | take the next peer |
| `rejected` | the best peer rejects the download | take the next peer |
| `stall` | the best peer's transfer stops at 30 % | stop it after 2 minutes, take the next peer |
| `wrongfile` | the best peer's file is a minute too long | leave it in slskd's folder, take the next peer |
| `slowsearch` | the search takes 30 s, answers arrive one by one | wait (a search is stopped after 45 s and what has arrived is used) |
| `exactfails` | only a search with a `*` wildcard finds something | the second, simpler search finds it |

Finished downloads land in `dev/slskd/releasomat/<download id>/` and are moved from there into
Releasomat's staging folder, then into `dev/library/Releasomat`. Only files in
`dev/slskd/releasomat/` are ever moved (ADR 0003); a wrong file stays there for you to look at.

The tests use the same fake, in-process and 100 times faster (`tests/downloaders/`), plus saved
answers shaped like slskd 0.26.1's (`tests/fixtures/online/slskd_*.json`, built from slskd's source,
not recorded yet).

**The real slskd** (the owner's) is only used for tests announced to the owner beforehand: at most
5 searches and 3 downloads per test, never from the automated tests. Its address and API key in the
dev app's Settings are the owner's; development uses the fake. Releasomat needs slskd 0.26 or newer
(download batches with a destination folder).

## YouTube downloads (yt-dlp and Deno)

YouTube needs yt-dlp, its helper yt-dlp-ejs and a JavaScript runtime (Deno). All three come with
`pip install -e '.[dev]'`: the dependency `yt-dlp[default,deno]` installs Deno from PyPI into
`.venv/bin/deno`, the same way the Docker image gets it (ADR 0007). Nothing to install by hand.
Check with:

```sh
.venv/bin/deno --version                                      # Deno 2.x
.venv/bin/python -c "import yt_dlp; print(yt_dlp.version.__version__)"
```

After `pip install -U "yt-dlp[default,deno]"` (a newer yt-dlp), the next download already uses it:
every download runs in a fresh Python process (`app/downloaders/youtube_job.py`). The Settings
and Activity pages show the versions in use.

Downloads first land in `dev/config/staging/` (cleaned up after each track and at start-up), then
move into `dev/library/Releasomat` as `Artist/Artist - Title.opus` (or `.m4a`), tagged and with a
cover. They are kept in YouTube's own format (no re-encoding).

The demo library's songs have their real lengths, so real playlists find them "already in
Navidrome" (Releasomat only links a song whose length is within 10 seconds of the playlist's
track). Files made by older versions of `scripts/make_demo_library.py` (5–8 seconds long) are
replaced the next time `scripts/dev_up.sh` runs; then let Navidrome scan again (`dev_up.sh` does).

Tests never run yt-dlp or ytmusicapi: they use fakes (`tests/test_youtube_downloader.py`).

### YouTube cookies

YouTube sometimes asks yt-dlp to "sign in to confirm you're not a bot". Then yt-dlp needs the
cookies of a logged-in browser: export them as `cookies.txt` (Netscape format, for example with a
browser extension) and save the file as `dev/config/cookies.txt` (local run) or
`dev/config-docker/cookies.txt` (container). Releasomat uses a copy of it for each download and
removes cookie values from error reports. Cookies work like a password: use a spare Google
account, and never commit the file (git ignores `dev/` and every `cookies.txt`).

## Stop and start over

```sh
scripts/dev_down.sh               # stop the test services; everything stays
scripts/dev_down.sh --reset       # stop and start over (shows what it deletes and asks first)
```

`--reset` empties the test Navidrome's database (`dev/navidrome-data`), Releasomat's databases
(`dev/config`, `dev/config-docker`) and the music Releasomat downloaded (`dev/library/Releasomat`).
It keeps `dev/.env`, `cookies.txt` and the demo library, and it
never touches anything outside `dev/`. Afterwards run `scripts/dev_up.sh` again.

## When something doesn't work

- **"bind source path does not exist"**: the `dev/` folders are missing. Run `scripts/dev_up.sh`
  (Docker is set up not to create them itself, because it would make them belong to root).
- **"port is already allocated"**: something else uses port 4533, 8430 or 8431. Stop it, or
  check with `docker ps` whether an old test container is still running.
- **"the login in dev/.env doesn't work"**: `NAVIDROME_PASSWORD` was changed after Navidrome
  created its admin. Put the old value back, or run `scripts/dev_down.sh --reset`.
- **Navidrome's log**: `docker compose -f docker-compose.dev.yml logs -f navidrome`
