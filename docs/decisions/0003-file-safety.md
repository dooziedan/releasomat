# 0003: Releasomat only touches its own files and never deletes on its own
Date: 2026-10-21, changed 2026-10-22, note 2026-10-25 · Status: Accepted

## Context
Releasomat writes into a folder that Navidrome (and Tagwerk) also read. Playlists like ListenBrainz's
Weekly Exploration change every week, so downloaded tracks drop out of playlists all the time. Losing
or overwriting music by mistake is the worst thing this app could do.

The first version of this decision let each playlist delete files of tracks that left it after a
waiting time. The owner decided against that on 2026-10-22: **tracks are only deleted by the owner.**

## Decision
- All file operations go through `app/files.py`. Nothing else creates, moves or deletes music files.
- Releasomat writes only inside `MUSIC_DIR`. Every path is resolved and checked to stay inside it
  (no `..`, no symlinks out). From `SLSKD_DOWNLOADS_DIR` it only moves files of downloads it started.
- It never overwrites an existing file: a name clash gets a numbered suffix.
- **Releasomat never deletes a music file on its own**: not on a refresh, not on a schedule, not when
  a track leaves a playlist, not when a playlist is removed. There is no setting that turns automatic
  deleting on.
- Deleting happens only when the owner presses a delete button for that file (a later phase: an
  overview of the download folder that shows which files no playlist uses any more), after a
  confirmation, and only for files Releasomat downloaded (`track_file.downloaded_by_releasomat`).
  If a playlist still contains the track, the confirmation says which.
- Songs that were already in Navidrome are linked, never downloaded again, moved or deleted.
- **Files leave Releasomat's folder only by the owner's action**, in Releasomat or in another
  program such as Tagwerk (adopt, delete; ADR 0009). Releasomat notices it through Navidrome on the
  next refresh: a file found elsewhere in the library is linked there, one found nowhere is marked
  "Don't like" and never downloaded again (unless the folder looks unmounted: then nothing is
  marked). Tagwerk's delete goes through Releasomat's own owner's delete, with the same checks.

## Consequences
- The download folder only grows until the owner cleans it up; the UI shows its size and which files
  no playlist uses any more.
- When a track leaves a playlist, it only leaves the Navidrome playlist; its file stays.
- Tests check that no refresh, schedule or playlist removal ever deletes a file.
