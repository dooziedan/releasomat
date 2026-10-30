# Roadmap

Each phase ends with a local preview and the owner's OK. Status: ✅ done · 🚧 in progress · ⬜ open.

## Phase 0: Foundation ✅
- ✅ Project skeleton: FastAPI app, SQLite + Alembic, settings (DB + env), base layout, `/health`
- ✅ Playlist list, add-by-URL with source detection, playlist page with per-playlist settings
  (schedule, Navidrome mode, downloaders), all stored but not yet doing network work
- ✅ Scheduler wiring (jobs rebuilt from the DB), run records, Activity page skeleton
- ✅ Docker image, entrypoint (PUID/PGID/UMASK), compose file, Unraid template
- ✅ CI: lint + tests on PRs, image + GitHub Release on tags
- ✅ Dev environment: Navidrome test instance with a demo library, `dev/.env`, launch configs, screenshots

## Phase 1: First complete path 🚧
Built; waiting for the owner's tests. Details and pitfalls: `docs/research/`.
- Navidrome client: connect/test, search (MBID first), targeted scan + wait, update playlists in
  place (keep the ID), find downloaded songs by MBID or Navidrome's own API (see architecture.md)
- ListenBrainz: own playlists and "Created for you" series (Weekly Exploration, Weekly Jams, Daily
  Jams); retry with backoff when ListenBrainz is down (it often was in 2026)
- Matching: MBID → ISRC → fuzzy (title, main artist, duration); keep mix names apart
- yt-dlp downloader: search YouTube Music (`filter="songs"`, check artist and duration), download
  without re-encoding, check the file afterwards, tag with MBID/ISRC + cover, path template
- YouTube in 2026 needs a JavaScript runtime: Deno comes from PyPI (`yt-dlp[default,deno]`); a
  PO-token provider wasn't needed. Each download runs in a fresh worker process (ADR 0007)
- Error reports with Copy report; live tracks table
- Open: how yt-dlp stays current in the container (ADR 0007 proposes a weekly image rebuild)
- Refresh now + scheduled refresh, live progress on the Activity page

## Phase 2: More sources 🚧
Built and read live once each; Tidal not yet tried with real credentials.

Apple Music (core), Deezer, Tidal, YouTube Music, YouTube, SoundCloud, Beatport (ADR 0006). No
Spotify. Every source failure ends in an error report.

## Phase 3: Download sources and Soulseek 🚧
Owner's decisions in ADR 0008.
- ✅ Global download order with on/off switches in Settings (no per-playlist choice); default
  Soulseek, then YouTube
- ✅ Fake slskd for development (`scripts/fake_slskd.py`): no Soulseek network, no account
- ✅ slskd downloader: one search at a time, format order + minimum bitrate, length check, next peer
  on refusal/stall/wrong file, finished file moved in (ADR 0003); AIFF/WAV tagging
- ✅ Real answers recorded from the owner's slskd 0.26.1 on 2026-10-23 (1 check, 5 searches,
  3 downloads: 2 FLAC succeeded, 1 refused by the peer). They showed that "01 - Title" and
  "Artist - Title" file names were taken for other versions; fixed, and the anonymised answers are
  now test fixtures (`tests/fixtures/online/slskd_real_*`)
- ⬜ Real end-to-end test where Releasomat and slskd share the download folder (Unraid)

## Phase 4: Lifecycle 🚧
- 🚧 Files after the download and the **optional** Tagwerk integration (ADR 0009; Releasomat works
  in full without it): states "adopted" and "Don't like", files that leave the folder (moved →
  linked, deleted → Don't like, safety brake for an unmounted folder), downloads that appear in one
  step, the integration API (`/api/integration/…`, off without a key), Settings → Tagwerk, badges
  and Undo on the playlist page; adopted songs keep their place in the Navidrome playlist and stay
  the same song (ADR 0010). Tagwerk's side follows separately (Tagwerk ADR 0044; the two-step
  import of ADR 0010 is still to do there); "Give back" later.
- ⬜ Download folder overview: size, files no playlist uses any more, a delete button per file (only
  by the owner, with confirmation, ADR 0003; marks the track "Don't like"); snapshot mode with
  "keep last N"; retry of failed tracks; offering a better copy of a YouTube download (the owner
  decides, the old file is only removed when they confirm).

## Phase 5: First release ⬜
Setup wizard (it never asks for the optional Tagwerk key), optional login for the UI, notifications, README with screenshots, v0.1.0 on Unraid.
The Unraid template points to `:edge` (every push to main) until then; switch it to `:latest`
with the first release.

## Ideas, not planned yet
- Hand downloads to Tagwerk's import inbox instead of (or in addition to) Releasomat's own folder
  (for now the owner adopts them by click in Tagwerk, ADR 0009).
- Beatport / Traxsource charts as sources.

## Not in scope
- Streaming or playing music (Navidrome does that).
- Editing tags of the existing library (Tagwerk does that).
