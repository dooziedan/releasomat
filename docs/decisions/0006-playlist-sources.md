# 0006: Playlist sources, no Spotify, an error report when a source fails
Date: 2026-10-22 · Status: Accepted (owner's decision)

## Context
Research in `docs/research/services.md` (checked 2026-10-21) found:
- **Spotify** no longer lets self-hosted apps read other people's playlists. Since March 2026,
  development-mode apps only get the tracks of playlists the logged-in user owns or collaborates on;
  the app owner needs Premium and at most 5 users per app. Without an API key, the public embed page
  shows at most 100 tracks. Explo imitates the web player with a secret fetched from a third-party
  server: fragile and against Spotify's terms.
- **Apple Music**: the public playlist page contains all tracks as JSON (300 of 300 tested), without
  ISRCs. It breaks when Apple changes the page.
- **Deezer** (public API) and **Tidal** (official API, developer app) give full playlists with ISRCs.
- **ListenBrainz** is official and gives MusicBrainz IDs, but was often slow or down in 2026.
- **YouTube Music / YouTube / SoundCloud** work through ytmusicapi / yt-dlp.
- **Beatport** pages contain track data with ISRC, BPM, key and mix name.

## Decision
- **Sources**: ListenBrainz (playlists and "Created for you"), **Apple Music** (a core source, always
  on), Deezer, Tidal, YouTube Music, YouTube, SoundCloud, Beatport.
- **No Spotify.** A Spotify link gets the normal "Releasomat doesn't know this site" message.
- All sources are equal in the UI: **no labels** like "official" or "unofficial", **no opt-in**.
  Each source works as soon as it is added; only Tidal needs its client ID and secret in Settings.
- **When a source doesn't work, there is an error report.** The run ends as failed and keeps a
  report that says, in plain language, what went wrong and where, plus the technical details needed
  to fix it (see `docs/architecture.md`, "Error reports"). The playlist keeps its last good state:
  nothing in Navidrome changes because of a failed read.
- No source fetches secrets from third-party servers at run time.

## Consequences
- Sources that read web pages (Apple Music, Beatport) need maintenance when the services change. The
  error report makes that visible right away, and saved answers in `tests/fixtures/online/` show
  what the page looked like when it last worked.
