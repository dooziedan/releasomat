# 0009: Releasomat and Tagwerk: a discovery area, adopted into the library by click
Date: 2026-10-24, decisions completed 2026-10-25 · Status: Accepted (owner's decisions)

## Context
Releasomat saves its downloads in its own folder inside the music library (ADR 0002), e.g.
`/mnt/user/music/Releasomat`. Tagwerk, the owner's library manager, scans the whole library, so it
sees those downloads as ordinary library tracks: it counts and checks them and may propose renames,
moves or the trash. If a Releasomat file disappears that way, Releasomat's next refresh downloads it
again. Tagwerk also has an import inbox for new music (Tagwerk ADR 0007) with identification,
BPM/key, genre folders, duplicates (0011) and better copies (0037).

The owner decided (2026-10-24):
- **Releasomat's folder is a discovery area.** Its playlists work right away; the curated DJ
  library stays Tagwerk's.
- **Tracks get into the library only when the owner adopts them in Tagwerk, by click.**
- **A Releasomat track the owner deletes is marked "Don't like":** Releasomat never downloads it
  again. It may still appear in playlists: the track stays in Releasomat's track list (marked), and
  if the song exists elsewhere in the library, it is linked into the Navidrome playlist as usual.

And (2026-10-25):
- **Tagwerk keeps the area out of the library after one confirming click** in its Settings, which
  first shows what leaves Tagwerk's lists (the files themselves stay untouched).
- **Tagwerk's Releasomat page offers Adopt and Delete.** Delete asks Releasomat to delete the file;
  Tagwerk itself never deletes area files.
- **Releasomat writes no note about where a file came from** into it: no comment, no field of its
  own. Normal tags stay (title, artist, album, cover, MusicBrainz ID, ISRC): Navidrome needs them to
  show the song, and Releasomat needs the IDs to find the song again after an adoption.
- **Give back** (an adopted file back from Tagwerk's inbox into the area) comes later.
- **The integration is optional.** Releasomat must work in full without Tagwerk, and Tagwerk
  without Releasomat; nobody is forced to run both. In Releasomat: with no `INTEGRATION_API_KEY`
  (the default) everything works as before (refreshes, downloads, playlists, files that leave the
  folder, found through Navidrome alone); nothing waits for, retries towards or calls Tagwerk
  (only Tagwerk calls Releasomat); the Settings group "Tagwerk" says it is optional and nothing
  warns about a missing key; a setup wizard never asks for it; "Adopted by Tagwerk" only shows for
  files Tagwerk actually adopted.

## Contract between the two apps

### Names
- **Releasomat area**: Releasomat's `MUSIC_DIR`, a folder inside the library. Tagwerk sees it as
  `/music/<folder>`; the folder name is Releasomat's `NAVIDROME_SUBFOLDER` (default `Releasomat`).
- **Adopt**: the owner's click in Tagwerk that moves a Releasomat file into Tagwerk's inbox.

### Tagwerk (details: Tagwerk ADR 0044, proposed)
- Settings: `RELEASOMAT_URL`, `RELEASOMAT_API_KEY` (Releasomat's `INTEGRATION_API_KEY`) and an
  optional `RELEASOMAT_FOLDER`; the folder name otherwise comes from `info` and is remembered.
- The area is **skipped by Tagwerk's library scan**: its files get no library rows, so nothing in
  Tagwerk can tag, rename, move, trash or count them. A small table of its own feeds the
  **Releasomat page** (files, quality, length, which Releasomat playlists contain them, "already in
  your library", ▶ to listen, **Adopt** and **Delete** per file and for many). Keeping the folder
  out works even before the integration API exists (`RELEASOMAT_FOLDER`), switched on once by the
  owner after a pre-check.
- **Adopt** (only with an import folder): Tagwerk calls `adopt` first; no answer or an error →
  nothing moves. Then it moves the file into its inbox (`/import/Releasomat/<path>`: copy to a
  hidden name, compare checksums, rename, remove the original). A failed move → `returned`. From
  there the normal inbox decides (identification, BPM/key, duplicates, better copies, review or
  automatic import). Releasomat's tags stay unless the owner changes them on the review page.
- Notices (`discarded` when the owner moves an adopted file to the inbox trash, `restored` when it
  comes back from there, `returned`) are kept and retried until Releasomat takes them.

### The integration API (Releasomat, version 1)
- Under `/api/integration/`, key in `Authorization: Bearer <INTEGRATION_API_KEY>`, never in a URL.
  No key set in Releasomat (or one shorter than 16 characters): the integration is off (403
  `integration_off`); a missing or wrong key: 401 `wrong_key`. The key is checked before the body,
  so a call without it never gets 422. Keys are never logged or echoed.
- Errors share one shape: `{"integration_api": 1, "error": "<code>", "message": "<plain words>"}`
  (also `bad_request` with 422 for a body that doesn't fit, `not_found`, `method_not_allowed`).
- `info` answers `{"app": "releasomat", "version": "…", "integration_api": 1, "folder": "…"}`.
  `integration_api` only goes up for changes an older partner can't handle; unknown fields are
  ignored by both apps.
- Files are named by their **path inside the area** (`/`, Unicode NFC, e.g.
  `Röyksopp/Röyksopp - Eple.opus`; `Releasomat/…` and `/music/Releasomat/…` are accepted too) plus
  their **size**; answers carry Releasomat's file `id`, used in later notices.
- `GET files`: the files in the area now (downloaded by Releasomat, still its own, on disk), each
  with `id`, `path`, `size`, `downloaded_at`, `downloader`, `format`, `bitrate`, `wanted` (artist,
  title, album, duration, ISRC, MusicBrainz ID, and BPM and key when the playlist source has them;
  genre and label once a source stores them) and `playlists` (`id`, `name`, `url` of the playlist's
  page).
- `POST adopt`, `returned`, `discarded`, `restored`, `delete`: each takes
  `{"files": [{"path": "…", "size": 123, "id": 12}, …]}` (`size` and `id` optional; `returned`
  also `"why": "move_failed" | "given_back"` next to `files`) and answers
  `{"integration_api": 1, "files": [...]}` with one entry per file, in the order sent: `asked` (the
  path as sent), `ok`, `changed` (false when it already was so), `reason` (plain words, when not
  ok), Releasomat's `id`, the stored `path` and the file's `status` now. Repeating a call that
  already went through answers `ok` again. `adopt` and `delete` refuse a file whose size differs
  from the one sent; `delete` refuses a file sent without its size (deleting can't be undone).
- After `adopt` said yes, Releasomat leaves that path alone; the file disappearing is expected.
- `delete` is the owner's delete, started on Tagwerk's Releasomat page: Releasomat deletes the file
  with its own owner-delete (`files.delete_by_owner`, ADR 0003: only files it downloaded, only in its
  folder) and marks the track "Don't like". It is the only call that touches a file. A file that is
  already gone and "Don't like" answers `ok`; a file that should be there but isn't changes
  nothing (the folder may be unmounted).
- **Downloads appear in one step**: Releasomat writes a new file under a hidden name in the target
  folder and renames it when complete (the final name reserved first, never overwriting), so
  neither Tagwerk nor Navidrome sees half a file.

### Releasomat (details: `docs/plans/tagwerk-integration-releasomat.md`)
- New file states:
  - **adopted**: handed to Tagwerk. Never downloaded again. Each refresh looks for the song in
    Navidrome outside Releasomat's folder (MusicBrainz ID, ISRC, name and length); once found it is
    linked as **in library** and the Navidrome playlist uses the library copy.
  - **disliked** ("Don't like"): the owner deleted it (in Releasomat, or threw it away in Tagwerk's
    inbox). Never downloaded again; the track stays in the playlist's track list with a badge; if the
    song exists elsewhere in the library it is still linked. **Undo** on the playlist page (the next
    refresh downloads it again).
- A file that disappears from the area without an adoption notice: found elsewhere in the library
  (the owner moved it) → linked as in library; not found → "Don't like", lifted by itself if the
  song turns up later. If the folder looks unmounted (missing, empty, or every expected file of a
  playlist gone at once, at least 5), nothing is marked and the run warns.
- `returned` puts a file back to its state before `adopt`; `restored` turns a Don't like from
  `discarded` back into adopted.
- ADR 0003 stays: Releasomat never deletes music on its own. Files leave its folder only by the
  owner's action, in Releasomat or in Tagwerk. The integration API touches files only for
  `delete`, the owner's delete started in Tagwerk.

## Consequences
- Both apps change; either can be updated first. Tagwerk's first step (keeping the folder out of the
  library) needs nothing from Releasomat.
- After an adoption the song's Navidrome ID usually changes (new path, Tagwerk's tags). Releasomat
  repairs its playlists on the next refresh by finding the library copy. While a track waits in
  Tagwerk's inbox it is missing from the Navidrome playlist.
- Tagwerk must be able to remove files from the area: both containers run with the same PUID/PGID.
- Until Tagwerk's first step ships, Tagwerk still treats the area as library: avoid Tagwerk actions
  on those files.
