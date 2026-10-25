# Releasomat: notes for Claude

Self-hosted playlist downloader: imports playlists from Apple Music, ListenBrainz, Deezer, Tidal,
YouTube (Music), SoundCloud and Beatport (no Spotify, ADR 0006), downloads missing tracks from Soulseek
(the owner's slskd) or YouTube (yt-dlp) in a global order (ADR 0008) and builds matching playlists in
Navidrome.
Python + FastAPI, deployed as one Docker container on Unraid next to Navidrome. Modelled on Explo
(github.com/LumePart/Explo), but a fresh codebase.
The owner is learning to code: explain changes in plain language and keep the code easy to read.
The owner writes in German: answer in German. Code, docs, commits and PRs stay in English.
The latest handoff notes are in `dev/handoff-*.md` (git-ignored): read the newest one first.

## Commands
- Setup: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
- Dev services (Navidrome test instance + demo library + `dev/.env`): `scripts/dev_up.sh`; stop with `scripts/dev_down.sh` (`--reset` wipes dev data)
- Run locally: `.venv/bin/uvicorn app.main:app --reload --port 8430 --env-file dev/.env` (http://localhost:8430)
- Tests: `.venv/bin/pytest`
- Lint/format: `.venv/bin/ruff check . && .venv/bin/ruff format .`
- Container: `docker compose up --build` (http://localhost:8430)
- New migration after model changes: `CONFIG_DIR=dev/config .venv/bin/alembic revision --autogenerate -m "…"`
- Screenshots of UI changes: `.venv/bin/python scripts/screenshots.py`
- Details: `docs/development.md`

## Rules
- **Files: Releasomat only touches its own files** ([ADR 0003](docs/decisions/0003-file-safety.md)). Only `app/files.py` creates, moves or deletes music files, only inside `MUSIC_DIR` (and moving its own finished downloads out of `SLSKD_DOWNLOADS_DIR`). Never overwrite. **Releasomat never deletes music files on its own** (no refresh, schedule, playlist change or setting does): only the owner deletes, by pressing a delete button for a file Releasomat downloaded, after a confirmation. Songs already in Navidrome are linked, never touched.
- Develop against the dev Navidrome and `dev/` folders, never the owner's real library or Navidrome.
- **Soulseek ([ADR 0008](docs/decisions/0008-download-sources-and-soulseek.md)): develop against the fake slskd (`scripts/fake_slskd.py`).** Don't set up a real slskd (dev service, image, account) without the owner's explicit permission. The owner's own slskd (address/API key in the dev app's Settings, never printed) may be used only for announced tests: **at most 5 searches and 3 downloads per test**, tell the owner before each one.
- **No origin note in music files** (owner's decision, ADR 0009): Releasomat writes normal tags only (title, artist, album, cover, MusicBrainz ID, ISRC); where a file came from stays in the database.
- **Releasomat never depends on Tagwerk** (owner's decision, ADR 0009): the integration API (`app/routes/integration.py`) is optional and off without a key (`INTEGRATION_API_KEY`); Releasomat works in full without it, never calls Tagwerk, and the UI never nags about a missing key.
- **The integration API touches files only for `delete`** (the owner's delete started in Tagwerk, through `lifecycle.delete_and_dislike` → `files.delete_by_owner`). Every other call only changes the database (`app/lifecycle.py`).
- **When a source or step fails, the run keeps an error report** (ADR 0006, architecture.md "Error reports"): plain-language what/where plus technical details, never secrets. A failed read changes nothing in Navidrome.
- Unraid compatibility ([ADR 0002](docs/decisions/0002-unraid-deployment.md)): PUID/PGID/UMASK (defaults 99/100/022), container paths `/config`, `/music`, `/slskd` (optional), port 8430. Reach Navidrome/slskd via host IP or Docker network, never `localhost`. Never `chown` mapped folders in bulk.
- **Everything must run inside the Docker image**: Python, ffmpeg, yt-dlp, ytmusicapi. No host tools at runtime. yt-dlp and ytmusicapi are used as Python libraries, not through their command line (downloads may run in a separate worker process so yt-dlp updates take effect; see roadmap Phase 1).
- Settings: container settings only from env vars (`app/config.py`); user settings in the DB (`app/settings.py`), where an env var with the same name wins and is shown read-only. Secrets are never logged and never rendered back in full.
- Sources implement `app/sources/base.py`, downloaders `app/downloaders/base.py`. Only `app/navidrome.py` talks to Navidrome. HTTP via httpx with a `Releasomat/<version> (+repo URL)` User-Agent, timeouts and each service's rate limit. **Tests never go online**: saved answers in `tests/fixtures/online/`.
- Schedules ([ADR 0005](docs/decisions/0005-scheduling.md)): the DB is the source of truth; `app/scheduler.py` rebuilds APScheduler jobs from it. A playlist never runs twice at once.
- Schema changes go through Alembic migrations. Never edit an already released migration. New NOT NULL columns need a `server_default`. `test_migrations_match_models` guards drift.
- UI: Jinja2 + htmx + Pico CSS, vendored in `app/static/`, no JS build step. The UI uses the same routes as `/docs`. Colours only via tokens in `app/static/theme.css`.
- **No `color-mix()` or other newer CSS for anything essential**: the owner's browser didn't apply it in Tagwerk (white-on-white). CSS custom properties are fine.
- Check UI changes with `scripts/screenshots.py` (Chromium, Firefox, WebKit) before a PR.
- Explo (MIT) is a reference for approaches, not a code source ([ADR 0004](docs/decisions/0004-license.md)). If anything is adapted, credit it in `NOTICE`.
- Every user-visible change: update `CHANGELOG.md` (Unreleased). New design decisions: add an ADR in `docs/decisions/`.
- **Local preview first:** run every change locally (http://localhost:8430 against the dev Navidrome) and wait for the owner's OK before committing and pushing.
- Work on feature branches with PRs; commit or push only when asked. **The owner merges PRs** (Claude Code refuses `gh pr merge` by Claude): open the PR, check CI with the ccd_pr tools, then ask. Every push to `main` publishes `ghcr.io/dooziedan/releasomat:edge`, which the owner runs on Unraid.
- The repo is public: commits use the repo-local GitHub noreply e-mail (`git config user.email`), never the owner's private address. Releases are git tags `vX.Y.Z`: CI publishes the image and creates the GitHub Release page from the version's `CHANGELOG.md` section, so write changelog entries for readers of that page.
- **Never put Claude session links (claude.ai/code/session…) or other private URLs/IDs in commits, PR descriptions or files.** Attribution is only `Co-Authored-By: Claude …` in commits and the "Generated with Claude Code" line in PRs.

## Layout
See `docs/architecture.md`. Plan and phase status: `docs/roadmap.md`. Research on Explo and the services: `docs/research/`.
