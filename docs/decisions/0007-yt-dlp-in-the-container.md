# 0007: yt-dlp, Deno and fresh worker processes
Date: 2026-10-22 · Status: Accepted (how yt-dlp stays current: proposal, see below)

## Context
- YouTube downloads in 2026 need **yt-dlp-ejs** and a **JavaScript runtime** (Deno ≥ 2.3 is the
  one yt-dlp enables by default) to solve YouTube's challenges; without them only some formats work.
- yt-dlp breaks within weeks when YouTube changes something, and an updated yt-dlp only takes
  effect in a Python process that imports it anew.
- Everything must run inside the Docker image, and the same setup should work in a local venv
  (CLAUDE.md). yt-dlp and ytmusicapi are used as Python libraries, not through their command line.

## Decision
- **Deno comes from PyPI**: the dependency is `yt-dlp[default,deno]`. `default` brings yt-dlp-ejs,
  `deno` the official Deno binary, published on PyPI by the Deno project (wheels for Linux
  x86_64 and aarch64, macOS, Windows). `pip install .` puts it in `/usr/local/bin/deno` in the
  image and `pip install -e '.[dev]'` in `.venv/bin/deno` locally; yt-dlp looks in Python's own
  scripts folder first, so it finds Deno without any PATH setup. No download script in the
  Dockerfile, no separate local install. The image build fails if Deno, yt-dlp-ejs or ytmusicapi
  is missing. Deno keeps its cache in `CONFIG_DIR/cache/deno`, yt-dlp in `CONFIG_DIR/cache/yt-dlp`.
- **Every download runs in a fresh worker process**: `python -m app.downloaders.youtube_job`,
  job in as JSON on stdin, answer out as JSON on stdout. Inside, ytmusicapi searches and yt-dlp
  downloads as libraries. At most `DOWNLOAD_CONCURRENCY` run at once; one that takes longer than
  15 minutes is killed. A new process per download (about a second of start-up) means an updated
  yt-dlp applies to the next download without restarting Releasomat. The worker runs in its own process
  group: **Stop** sends it SIGTERM and, after 3 seconds, SIGKILL, which also ends the ffmpeg and
  Deno processes yt-dlp started. (A spawn-based process pool
  was tried first; it re-imports the server's main module in every child, which is fragile.)
- **No PO-token provider for now.** Tested on 2026-10-22 with yt-dlp 2026.10.21 and Deno 2.9.8,
  locally and in the container: search, challenge solving and downloads worked without one. A
  provider (bgutil-ytdlp-pot-provider) is a separate HTTP server with a history of security holes;
  it is added only if downloads start failing with "Sign in to confirm you're not a bot" or
  HTTP 403 regularly.
- Audio is downloaded **as YouTube serves it** (usually Opus, sometimes M4A). ffmpeg only moves it
  into its usual container (`.opus`, `.m4a`), it never re-encodes. Each file is checked (readable
  audio, length within 10 s) before it is tagged and saved; otherwise the next candidate is tried.
- Optional `CONFIG_DIR/cookies.txt` is used as a copy (yt-dlp rewrites cookie files), and there is
  a 5-second pause between downloads in the same slot.
- The yt-dlp and Deno versions are shown on the Activity and Settings pages, in `/api/status` and
  in every error report.

## Keeping yt-dlp current (proposal for the owner)
1. **Now:** `pyproject.toml` only sets a minimum (`yt-dlp>=2026.10.21`), so every image build gets
   the newest yt-dlp and Deno. An image is as fresh as its build.
2. **Proposed next step:** a scheduled CI workflow that rebuilds and publishes the image weekly
   (and on demand), so updating the container on Unraid brings the newest yt-dlp. Simple, tested
   by CI, no code downloaded at run time.
3. **Possible follow-up (in-app update):** a daily job (or a button) runs
   `pip install --upgrade --target CONFIG_DIR/python "yt-dlp[default]"`, and the worker process
   puts that folder first on its `PYTHONPATH`. Thanks to the fresh worker processes the next
   download uses it, without a restart. "Back to the image's version" deletes the folder. This
   downloads code at run time from PyPI, so it should be a setting the owner turns on.

## Consequences and risks
- The image grows by about 92 MB (Deno). ffmpeg's libraries are still the biggest part.
- YouTube can block anyway: bot checks, HTTP 403/429, about 300 videos per hour without an
  account. The error report says so in plain words and suggests cookies.txt, which carries the
  risk of the account being banned (yt-dlp's wiki), so a spare account is recommended.
- Downloading from YouTube may be against YouTube's terms; the owner decides what to download.
- Opus files don't play in some DJ software (Rekordbox, Serato, Traktor). If that matters, a
  per-playlist "prefer M4A" choice is a small follow-up (format `bestaudio[ext=m4a]/bestaudio`).
- yt-dlp-ejs is pinned by yt-dlp itself (each yt-dlp release names its yt-dlp-ejs version).
