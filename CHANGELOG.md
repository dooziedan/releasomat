# Changelog

All notable changes to Releasomat. Versions follow [Semantic Versioning](https://semver.org/).
Each version's section becomes the text of its GitHub Release page.

## Unreleased

### Changed
- ListenBrainz "Created for you" playlists (Weekly Exploration, Weekly Jams, Daily Jams) are named
  without their week or day, e.g. "Weekly Exploration for rob": they always show the newest one.
  Navidrome playlists that still carry an old date in their name get the new name with the next
  refresh; a name you gave a playlist in Navidrome yourself stays.
- Releasomat's folder inside the library is now called `releasomat` by default (lowercase, was
  `Releasomat`), also in the Unraid template's example. If yours is still called `Releasomat`,
  rename it (and the container's music folder path) or enter `Releasomat` in Settings → Navidrome.

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
- A tracks table on each playlist page that updates live during a refresh. A failed or missing
  track says why (one line per download source); a track being downloaded shows which source
  it is trying, without the reason an earlier try failed.
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
- Files that leave Releasomat's folder: a song whose file you delete is marked "Don't like" and
  never downloaded again (it is still linked if the song is elsewhere in your library); a file you
  move into your library is linked there. If Releasomat's folder looks unmounted (missing, empty,
  or every file of a playlist gone at once), nothing is marked and the run says so.
- New downloads appear in Releasomat's folder in one step (copied under a hidden name and renamed
  when complete), so Navidrome never sees half a file.
- "Don't like" on the playlist page: tracks you deleted are marked with a badge, say why, and have
  an **Undo** button (the next refresh downloads them again). The playlist list shows how many
  tracks are adopted or "Don't like", when there are any.
- Optional: working together with Tagwerk, if you use it to manage your library. Releasomat works
  in full without it; nothing changes until you set a key in **Settings → Tagwerk** (a
  **Generate** button makes one). With a key, Tagwerk can list Releasomat's downloads, adopt them
  into your library (Releasomat then never downloads them again and links the library copy once
  Tagwerk has imported it) and delete ones you don't like. The key is sent as
  `Authorization: Bearer …`; the calls are documented at `/docs`.
- Stop button for a running refresh (playlist page and Activity): the current download ends right
  away, tracks already downloaded stay, and the Navidrome playlist is left as it is.
- Docker image for Unraid with an Unraid template.
