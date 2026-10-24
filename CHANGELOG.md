# Changelog

All notable changes to Releasomat. Versions follow [Semantic Versioning](https://semver.org/).
Each version's section becomes the text of its GitHub Release page.

## Unreleased

### Added
- Follow playlists from ListenBrainz (including "Created for you": Weekly Exploration, Weekly Jams,
  Daily Jams), Apple Music, Deezer, Tidal, YouTube Music, YouTube, SoundCloud and Beatport by
  pasting a link. The Add playlist page shows every supported service and highlights the one it
  recognises.
- Refresh now and per-playlist schedules: songs you already have in Navidrome are linked, missing
  ones are downloaded from YouTube (checked to be the right song, tagged with cover art) and the
  playlist is created in Navidrome, in the same order.
- One Navidrome playlist kept up to date (same playlist, so apps that sync playlists don't see a new
  one each time), or a new dated snapshot per refresh.
- Setting "Delete old snapshot playlists automatically" (off by default): old snapshots stay until
  you delete them, or Releasomat keeps the newest few per playlist. Only playlists it created
  itself are ever deleted; music files never.
- A tracks table on each playlist page that updates live during a refresh.
- Error reports: when a service, a download or Navidrome fails, the run says what went wrong in
  plain words, with the details needed to fix it and a Copy report button. A failed read changes
  nothing in Navidrome.
- Releasomat never deletes music files on its own.
- Settings → Download sources: choose which sources (Soulseek, YouTube) download missing tracks
  and in which order, for all playlists; for Soulseek also the wanted formats and the minimum
  bitrate, and a Test button.
- Soulseek downloads through your own slskd: Releasomat picks the best file by your format order
  and minimum bitrate, checks its length and tries the next person when a download is refused,
  stalls or turns out to be the wrong song.
- AIFF and WAV downloads get tags and cover art too.
- Stop button for a running refresh (playlist page and Activity): the current download ends right
  away, tracks already downloaded stay, and the Navidrome playlist is left as it is.
- Docker image for Unraid with an Unraid template.
