# 0008: One global download order, Soulseek developed against a fake slskd
Date: 2026-10-23 · Status: Accepted (owner's decisions)

## Context
The owner wants to decide which download sources Releasomat uses and in which order, with Soulseek
first. Soulseek is reached through slskd, a separate program the owner runs. Testing against the
real Soulseek network all the time would use the owner's account and other people's uploads, and
the owner asked not to set up anything Soulseek without permission.

## Decision
- **One global setting for all playlists**, no per-playlist downloader choice: `DOWNLOAD_SOURCES`,
  the enabled download sources in order (default `slskd,youtube`). Settings → "Download sources"
  shows every source with an on/off switch and up/down buttons. A source that isn't connected
  (Soulseek without address and API key) is skipped with a note in the run. With no source enabled,
  Releasomat only links songs that are already in Navidrome.
- **Soulseek quality rules** (global): `SLSKD_FORMATS`, the wanted formats in order (default
  `flac,aiff,wav,mp3`), and `SLSKD_MIN_BITRATE` for lossy files (default 320 kbit/s). A track whose
  Soulseek results don't meet them goes to the next source (YouTube).
- **Releasomat never runs slskd.** It talks to the owner's slskd through its API (address + API key)
  and moves finished downloads it started out of slskd's download folder (`/slskd`).
- **Development uses a fake slskd** (`scripts/fake_slskd.py`): it answers like slskd's API, has no
  connection to the Soulseek network, needs no account and no slskd image, and "delivers" files
  from the demo library. It plays through problem cases on purpose: no results, only low quality,
  peer offline, stalled transfer, wrong file.
- **The owner's real slskd** is used sparingly: once to record real answers (peer user names made
  anonymous) so the fake and the test fixtures match reality, then only at milestones. Budget per
  test: **at most 5 searches and 3 downloads**, announced to the owner beforehand. Those files stay
  in the owner's slskd folder; the owner decides what to keep.

## Consequences
- Soulseek logic is tested offline, repeatably, including failures that are rare in real life.
- Moving a finished Soulseek download into Releasomat's folder can only be tested for real where
  Releasomat and slskd share the folder (on Unraid); locally the fake covers it.
- Changing the order or the formats applies to the next refresh of every playlist.
