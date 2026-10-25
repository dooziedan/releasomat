# Tagwerk integration: the Releasomat side (plan)

Status: plan, 2026-10-24. The contract and the owner's decisions are in
[ADR 0009](../decisions/0009-tagwerk-integration.md). Tagwerk's side is planned separately against
the same contract (Tagwerk ADR 0044, proposed). Section 6 lists the contract changes this plan needs.

## Changed after Tagwerk's sketch (2026-10-24)
The contract in ADR 0009 now follows Tagwerk's requests where they differ from this plan:
- the key goes in `Authorization: Bearer …` (not `X-Api-Key`); the version field is
  `integration_api` (not `api`);
- `adopt`, `returned`, `discarded`, `restored` take lists of files (path + size) and answer per file;
  `adopt/cancel` below becomes `returned`, and `restored` is new (undo of `discarded`);
- `files` lists only files present in the area and adds `size`, `downloaded_at` and `wanted`
  (incl. BPM, key, genre, label when the source has them: needs those fields stored on `track`);
- `files.save_download` writes under a hidden name and renames when complete (one more step 0).
- owner's decisions of 2026-10-25: a `delete` call (Tagwerk's Delete button → `files.delete_by_owner`
  + Don't like, the only call that touches a file); **no origin note in files** (drop the
  "Downloaded by Releasomat from …" comment in `pipeline.keep()`; the origin stays in the database);
  Give back later.

## In short
Releasomat gets two new file states. **adopted** means Tagwerk has taken the file. **disliked**
("Don't like") means the owner deleted it. Releasomat never downloads either kind again, but it still
links them when the song shows up elsewhere in the library. On each refresh Releasomat checks the
files it downloaded. When one has disappeared without an adopt notice, it looks for the song
*outside* its own folder: found means link it, not found means Don't like. A small key-protected API
(`/api/integration/…`) lets Tagwerk say "I'm adopting this" and "the owner threw this away". The
integration API never touches music files; it only records what the owner did in Tagwerk (ADR 0003
stays as it is).

New module **`app/lifecycle.py`** holds every state change: adopt, cancel, discard, file left the
folder, library copy found, Don't like, Undo. The API routes, the refresh and the UI all call it, so
the rules live in one place. It works only on the database, never on files.

## 1. Integration API (`app/routes/integration.py`)
- Router `APIRouter(prefix="/api/integration", tags=["integration"],
  dependencies=[Depends(integration_key)])`, included in `app/main.py`.
- Each call has a Pydantic request/answer model, so `/docs` shows the shapes. `openapi_tags` adds:
  "For Tagwerk. Send the key in the X-Api-Key header (Settings → Tagwerk)."
- **Auth** (`integration_key()`):
  - Header via `fastapi.security.APIKeyHeader(name="X-Api-Key", auto_error=False)` (gives `/docs`
    its "Authorize" lock).
  - Empty `INTEGRATION_API_KEY`: **integration off**, every call answers 403 `integration_off`.
  - Missing or wrong key: 401 `wrong_key`, checked with `secrets.compare_digest`.
  - A router dependency, so it runs before the body is checked: no key gives 401, never 422.
  - The key is never logged or echoed; as a `secret` setting `reports.SECRET_SETTINGS` scrubs it, and
    `reports._PATTERNS` already hides `x-api-key:` headers.
  - After a successful check, `request.app.state.tagwerk_seen = (utcnow(), "adopt")` is kept in
    memory for the Settings page.
- **Errors** share one shape: `{"api": 1, "error": "<code>", "message": "<plain words>"}`, from a
  small `IntegrationError(status, code, message)` and an exception handler. A malformed body still
  gets FastAPI's 422.
- **Versioning**: `info` answers `"api": 1`; requests may send `"api": 1`, an unknown version gets
  400 `unsupported_api`. New optional fields don't change the version; both apps ignore unknown
  fields.

| Call | Body | Answers |
|---|---|---|
| `GET /info` | – | 200 `{"api":1,"app":"releasomat","version":"0.1.0","folder":"Releasomat","files":37}` (folder = `NAVIDROME_SUBFOLDER`) |
| `GET /files` (`?path=` optional) | – | 200 `{"api":1,"folder":"Releasomat","files":[F…]}`; with `?path=` and no match: 404 `unknown_file` |
| `POST /adopt` | `{"api":1,"path":"…"}` | 200 `{"result":"adopted"\|"already_adopted","file_id":12,"path":"…","artist":"…","title":"…"}`; 404 `unknown_file`; 409 `not_adoptable`; 422 `bad_path` |
| `POST /adopt/cancel` (new, §6) | same | 200 `taken_back` / `already_back`; 409 `file_not_there` |
| `POST /discarded` | same | 200 `disliked` / `already_disliked`; 404 `unknown_file`; 409 `not_adopted` |

- **File entry F**: `{"file_id", "path", "present", "artist", "title", "album", "duration", "isrc",
  "mbid", "format", "bitrate", "downloader", "playlists": [{"id", "title", "page"}]}`. `files` lists
  rows with `downloaded_by_releasomat` and a `path` (files that are still Releasomat's); `playlists`
  only those that still contain the track (one joined query).
- **Paths** (`lifecycle.find_by_path(session, raw, subfolder)`): refuses `..`, NUL and empty
  (`BadPath`, 422); Unicode NFC; candidates = as given, without a leading `Releasomat/`, and for
  absolute paths the part after each `/Releasomat/`; compared with NFC'd `path` and `former_path`.
  Two rows match: 409 `ambiguous`; none: 404. Never guesses; answers give the stored path back.
- **Idempotency**: `adopt` on `adopted` (or `in_library` with `adopted_at`) answers
  `already_adopted`; on `disliked` with reason `vanished` (a late notice) it becomes adopted; on an
  owner-disliked row (`deleted`/`owner`) 409. Repeated `discarded` answers `already_disliked`. Every
  change is a conditional update ("only if still `done` at this path").

## 2. Data model (`app/models.py`, migration `0004_tagwerk_adopted_disliked.py`)
- New `FileStatus` values `ADOPTED = "adopted"` (handed to Tagwerk, waiting for the library copy)
  and `DISLIKED = "disliked"` (Don't like, not in the library).
- **Statuses plus timestamps**: the status says where the song is now (the badge); timestamps
  remember what happened, so a disliked or adopted track can also be `in_library` once linked.
- New nullable `trackfile` columns: `former_path` (indexed), `adopted_at`, `disliked_at`,
  `disliked_reason` (`DislikeReason`: `vanished`, `deleted`, `discarded`, `owner`).

| Event (`lifecycle.…`) | status | path → former_path | downloaded_by_releasomat | navidrome_song_id | stamps |
|---|---|---|---|---|---|
| `adopt` | adopted | moved over | False | None (old in `details.old_song_id`) | adopted_at |
| `cancel_adopt` (file still there) | done | moved back | True | old ID | adopted_at = None |
| `link_library_copy` | in_library | – | False | new ID | kept; a `vanished` Don't like is lifted |
| `left_folder`, found elsewhere | in_library | moved over | False | new ID | `details.moved = true` |
| `left_folder`, not found | disliked | moved over | False | None | disliked_at, `vanished` |
| `discard` | disliked (stays in_library if linked) | – | False | kept | disliked_at, `discarded` |
| `mark_disliked` (buttons) | disliked | moved over if any | False | None | reason `deleted`/`owner` |
| `undo_dislike` | pending (stays in_library if linked) | – | – | – | disliked_* = None, adopted_at = None |
| linked song gone | `status_after_song_gone()`: disliked / adopted / pending | – | – | None | – |

`delete_by_owner` stays safe through `downloaded_by_releasomat=False` and `path=None`: Releasomat
can never delete a library copy.

## 3. Pipeline (`app/pipeline.py`, `app/navidrome.py`, `app/playlists.py`)
- **Navidrome**: `Navidrome.song_info(song_id)` (native `GET /api/song/{id}`: real `path`,
  `missing`); `Navidrome.find_elsewhere(track, threshold, skip_ids=())` = `find_in_library` whose
  best matches are checked with `song_info` (skip missing songs and paths under `f"{subfolder}/"`,
  up to 3 tries; if the native call fails, only skip `skip_ids` and log at INFO).
- **(a) Files that left the folder**: in `Refresh.match()` the "downloaded again" branch goes away.
  `done` + `downloaded_by_releasomat` + `path` rows failing `_exists()` go to
  `Refresh.files_that_left(gone)` before the search loop.
  - **Safety brake**: `MUSIC_DIR` missing or empty, or every expected file of this playlist gone and
    at least 5: nothing is decided; warning "Releasomat's folder looks unmounted; nothing was marked
    Don't like".
  - Else `find_elsewhere(track, skip_ids={old_id})`, then `lifecycle.left_folder(...)` as a
    conditional `UPDATE … WHERE status='done' AND path=:p` (an adopt notice in between wins).
- **(b) Relinking**: `adopted`/`disliked` rows keep their status in `match()`; the search uses
  `find_elsewhere` for them; a hit means `lifecycle.link_library_copy()`.
- **(c) Never download them**: `KEPT_AWAY = (ADOPTED, DISLIKED)` filtered in `Refresh.download()`;
  `save_file()` leaves these rows alone unless the caller sets `status`; `forget_song` uses
  `status_after_song_gone()`.
- **(d) Navidrome playlist**: relinked IDs go into `update_playlist()` (ID kept); `check_playlist()`
  repairs IDs that vanish later. Old snapshots stay frozen.
- **(e) Counts and messages**: `playlists.track_counts()` gets `adopted` and `disliked`; summary e.g.
  "12 tracks: 5 in your library, 4 downloaded, 1 adopted (waiting for your library), 1 Don't like,
  1 missing, 0 failed." plus "2 adopted into your library." / "1 moved into your library." / "1
  marked Don't like (its file was deleted from Releasomat's folder)."

## 4. UI
- Badges "Adopted by Tagwerk" and "Don't like" (`app/templating.py`, `app/static/app.css`, existing
  tokens only); "Where from" texts per state in `partials/tracks.html`; tracks API gains
  `adopted_at`, `disliked_at`, `disliked_reason`, `former_path`.
- **Undo** next to every Don't like: `DELETE /api/tracks/{track_id}/dislike`; page
  `POST /playlists/{pid}/tracks/{tid}/undo-dislike` (htmx swaps `#tracks`).
- Per-track actions: none for songs that were already in the library. The Phase-4 delete button
  calls `files.delete_by_owner()` then `lifecycle.mark_disliked(reason="deleted")`. *Optional (owner
  decides)*: "Don't like" for missing/failed tracks.
- **Settings** group "Tagwerk": `INTEGRATION_API_KEY` (secret, min. 16 characters), a **Generate**
  button (`secrets.token_urlsafe(32)`, not saved until "Save settings"), a status box (on/off, the
  folder for Tagwerk, the address for Tagwerk, last call from Tagwerk). Playlist list shows
  "· 2 adopted · 1 Don't like". Unraid template field `INTEGRATION_API_KEY` (advanced, masked).

## 5. Tests (never online)
- `tests/test_integration_api.py`: off/401/403, no 422 before auth, key never in answers or logs,
  shapes, path variants (NFD/NFC, prefixes, `..`), idempotency of adopt/cancel/discarded, the API
  never changes files.
- `tests/test_lifecycle.py`: one test per row of the table in §2, incl. conditional updates.
- `tests/test_pipeline.py`: deleted file → Don't like (rewrites today's "downloaded again" test),
  moved into the library → linked, stale Navidrome copy ignored, unmounted folder marks nothing,
  adopted never downloaded and linked once imported, disliked not downloaded but linkable, guessed
  Don't like lifted, Undo downloads again, adopt notice during a refresh wins.
- `tests/test_navidrome.py`: `song_info`, `find_elsewhere` with fixtures recorded from the **dev**
  Navidrome; fallback when the native API fails.
- Settings (minimum length, Generate saves nothing, masking), `/docs` lists the integration routes,
  migration drift.

## 6. Risks and contract changes
Risks: unmounted share (brake), a file moved before Navidrome scanned (guessed Don't like, lifted
later), stale Navidrome entries (real paths checked), adopt-vs-refresh races (conditional updates),
the gap while an adopted track waits in Tagwerk's inbox (it leaves the Navidrome playlist for a
while), permissions (same PUID/PGID for both containers), the key isn't a login for the whole app
(Phase 5), Don't like is per track row (optionally also block by `match_key`), future Phase-4 work
must respect `KEPT_AWAY`.

Contract changes proposed for ADR 0009: paths inside the area with `/` in NFC (also accepting
`Releasomat/…` and `/music/Releasomat/…`); `X-Api-Key` header, 403 when off, 401 when wrong;
`"api": 1`; one file per call, idempotent; adopt answers 200 before Tagwerk moves anything, 404 for
files Releasomat didn't download; `POST /api/integration/adopt/cancel` when Tagwerk's move fails;
`discarded` only for files the owner throws away (duplicates/better copies are not reported);
Releasomat only looks outside its own folder, brakes when its folder looks unmounted, and lifts a
guessed Don't like when the song turns up; consequences: the gap in the Navidrome playlist while a
track waits in Tagwerk's inbox, and the same PUID/PGID for both containers.

## 7. Order of work (each step a PR, previewed locally, OK'd by the owner)
1. **Track states**: models, migration 0004, `app/lifecycle.py`, `KEPT_AWAY`, `save_file` guard,
   counts.
2. **Files that leave the folder**: `song_info`, `find_elsewhere`, `files_that_left`, relinking,
   brake, messages (verify by deleting/moving files in `dev/library/Releasomat`).
3. **Integration API**: setting, Unraid field, routes, error handler, `/docs`, ADR 0009 wording,
   architecture.md, CHANGELOG.
4. **Badges, Undo, Settings**: tracks table, playlist list, Undo, Generate, status box, screenshots.
5. *(Optional)* "Don't like" for missing tracks; the delete button comes with Phase 4.
