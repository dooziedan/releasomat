# 0001: Python, FastAPI, SQLite, server-rendered pages with htmx
Date: 2026-10-21 · Status: Accepted

## Context
Releasomat is modelled on Explo, which uses Go for the backend and a React single-page app for the UI.
The owner asked for a stack that is fast, light in the browser and flexible. The owner is learning to
code and already runs Tagwerk (Python + FastAPI + htmx) next to Navidrome.

The work Releasomat does is mostly waiting: on HTTP APIs, on downloads, on ffmpeg and on Navidrome's
scan. Its two most important tools are written in Python: **yt-dlp** (YouTube downloads) and
**ytmusicapi** (YouTube Music search). Explo therefore ships two runtimes in its image (a Go binary
plus Python) and calls Python through subprocesses.

## Decision
- **Python 3.14 + FastAPI (async).** yt-dlp and ytmusicapi run as libraries in the same process: real
  progress hooks and error types instead of parsing subprocess output, and one runtime in the image.
  mutagen (tags), rapidfuzz (matching) and httpx (HTTP) cover the rest.
- **SQLite** via SQLModel, with Alembic migrations. One file in `/config`.
- **Jinja2 + htmx + Pico CSS**, vendored in `app/static/`. No JavaScript build step, no CDN. Live
  progress uses Server-Sent Events through htmx's SSE extension.
- **APScheduler 3** for per-playlist schedules (ADR 0005).

## Why not Go + React
- The browser gets finished HTML and about 15 KB of htmx instead of a React bundle that has to be
  downloaded, parsed and kept in memory. That is lighter on every device, including phones.
- Go would still need Python inside the image for yt-dlp and ytmusicapi.
- One language across Releasomat and Tagwerk: the owner can reuse what they learned, and patterns
  (Docker, Unraid template, CI, changelog-driven releases) carry over.

## Consequences
- The container uses more memory than a Go binary would (roughly 80–150 MB instead of 20–40 MB). On
  Unraid that is acceptable.
- CPU-heavy work (ffmpeg) runs in subprocesses, so Python's speed is not the bottleneck.
- A richer UI later could use the same JSON API without changing the backend.
