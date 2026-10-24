# Explo as a reference for Releasomat

*Research notes, written 2026-10-21.*

## What this document is

Explo (github.com/LumePart/Explo) is the closest existing app to what Releasomat
wants to be. I read its source code at tag **v1.2.0** (commit `5144986`, dated
2026-09-09) and its last 100 GitHub issues. This document explains how Explo
works, where it struggles, and what Releasomat should copy or do differently.

Every reference like `src/client/subsonic.go:128-189` points to a file and line
range in upstream Explo v1.2.0. If you want to look at the code yourself, clone
that tag. Line numbers will drift in later versions.

**Licence note.** Explo is MIT-licensed, Go + React. Releasomat is a new Python
codebase under AGPL-3.0-or-later. This document describes *approaches*. It
contains no Explo code, and Releasomat should not copy any either; write
Releasomat's own code from the ideas.

---

## 1. The big picture: how one Explo run works

Explo is **one Go program with two modes**:

1. **Web UI mode** (`WEB_UI=true`). A small web server with a setup wizard,
   settings, playlist cards and a log viewer. It does not download anything
   itself.
2. **Run mode** (`explo --playlist weekly-exploration ...`). This does the
   actual work, once, and then exits. Cron starts it on a schedule. The
   web UI also starts it when you press "Run".

One run, step by step (`src/main/main.go:127-250`):

1. Read config from a `.env` file plus command-line flags.
2. Get the track list:
   - for ListenBrainz playlists, ask ListenBrainz (section 2);
   - for imported ("custom") playlists, read a cached JSON file that the web UI
     wrote earlier (`loadCustomTracks`, `main.go:31-90`). The run never
     contacts Spotify or Apple Music itself.
3. If "replace playlist" is on, delete the old playlist in the music server
   (`main.go:221-226`).
4. If `--clean-downloads` is set, delete old downloaded files (section 8,
   `main.go:227-229`).
5. Ask the music server which tracks already exist (`client.CheckTracks`,
   `main.go:230-234`).
6. Download the missing tracks (`downloader.StartDownload`, `main.go:236-242`).
7. Trigger a library scan, wait for it, search the library again to get song
   IDs, and create the playlist (`client.CreatePlaylist`, `main.go:244-249`).

Explo keeps very little state. There is no database. The track list for the UI
lives in `config/cache/<playlist>.json`, custom playlists in
`config/custom-playlists.json`, and schedules and flags as lines in the `.env`
file. Explo has no record of which files it downloaded for which playlist. That
gap is the root of its file-cleanup problems (section 8).

---

## 2. ListenBrainz: how Explo gets recommendations

File: `src/discovery/listenbrainz.go`. Base URL `https://api.listenbrainz.org/1/`
(`lbRequest`, lines 773-786).

**Auth.** If the user gives a ListenBrainz token, Explo sends the header
`Authorization: Token <token>` on every request (`NewListenBrainz`, lines
196-207). The token is optional. The "created for" playlists are public, so
they also work without one. A token gives higher rate limits.

Explo has three ways to get tracks (`QueryTracks`, lines 208-266):

| Mode | Endpoint | What it gives |
|---|---|---|
| `playlist` (default) | `GET user/{user}/playlists/createdfor?offset=N`, then `GET playlist/{mbid}` | The real "Created for you" playlists |
| `api` | `GET cf/recommendation/user/{user}/recording` | A raw list of recommended recording MBIDs (fewer tracks, mostly for testing) |
| `on-repeat` | `GET stats/user/{user}/recordings?count=30&range=month` | The user's 30 most-played tracks this month |

### Finding the right "created for" playlist (`getImportPlaylist`, lines 594-646)

- It pages through `createdfor` (ListenBrainz returns 25 per page by default)
  until it has seen all playlists (lines 636-639). Issue #92 showed why paging
  matters: an older version only read the first 25 and missed playlists.
- Every playlist carries a field `extension → additional_metadata →
  algorithm_metadata → source_patch`. Its value is the playlist type, such as
  `weekly-exploration`, `weekly-jams` or `daily-jams`. Explo keeps only playlists
  whose `source_patch` matches the wanted type and picks the one with the newest
  `date` (lines 625-635).
- If the request fails, it retries up to 5 times with a **5-minute** sleep
  between tries (lines 603-614). Issues #89, #108 and #138 show ListenBrainz
  being slow or down, so a retry is needed. A 25-minute blocking wait inside a
  run is clumsy, though.

### Reading the playlist (`parsePlaylist`, lines 679-749)

`GET playlist/{mbid}` returns **JSPF**, a JSON playlist format. For each track
Explo takes:

- `title`, `creator` (artist credit), `album`, `duration` (ms);
- `identifier[0]`, a URL ending in the **recording MBID** (MusicBrainz ID). That
  is the most valuable field;
- `extension → additional_metadata → artists[]` (each with `artist_credit_name`
  and `artist_mbid`) to find the main artist and the featured artists;
- `caa_id` and `caa_release_mbid` to build a Cover Art Archive URL.

With `SINGLE_ARTIST=true`, Explo moves featured artists into the title
("Song (feat. B & C)") and keeps only the first artist as artist
(`buildTrackTitle`, lines 751-770).

### Optional enrichment (`enrichTracks`, lines 394-591)

With `ENRICH_TRACK_METADATA=true`, Explo makes one batch call to
`metadata/recording/?recording_mbids=...&inc=release artist tag release_group recording`.
After that it calls **MusicBrainz** (`/ws/2/recording/{mbid}?inc=media+releases+...`)
once per track, sleeping 2 s before each call (lines 486-497). This gives
track number, disc number, release country, genres (top 3 tags) and ISRCs. For
50 tracks it adds about two minutes.

### Things to note

- ListenBrainz timezone confusion (#89, #108, #119) was a frequent support
  topic. In v1.2.0 the code simply takes the newest playlist of the right type,
  so the timezone no longer matters for *finding* it.
- Daily Jams are only generated if the user follows `troi-bot` on ListenBrainz
  (see `services.md`).

---

## 3. Importing Spotify and Apple Music playlists

Files: `src/web/backend/playlist/spotify.go`, `apple_music.go`,
`custom_playlists.go`, `handlers.go`. The **web UI** does the import, not the
run. When you paste a URL, the UI fetches the tracks and saves them to
`config/cache/custom-<id>.json` (`HandleImportCustomPlaylist`,
`handlers.go:58-197`).

### Spotify: no official API, the web player is imitated

Explo does **not** use the official Spotify Web API and needs **no API key**.
It pretends to be the Spotify web player (`spotify.go`):

1. Load `open.spotify.com` to get cookies, a device ID, the client version
   (from a base64 `appServerConfig` script tag) and the URL of the web player's
   JavaScript bundle (`visitHome`, lines 238-310).
2. Get an anonymous access token from `open.spotify.com/api/token`. This needs
   a **TOTP code**, like a 2-factor code. It is computed from a secret that
   Spotify hides in its web player and changes from time to time. Explo
   downloads the current secret from a **third-party Git repository**
   (`code.thetadev.de/ThetaDev/spotify-secrets`) and falls back to a hard-coded
   copy (`getSpotifyTOTPSecret`, lines 69-124; `fetchAccessToken`, 313-357).
3. Get a "client token" from `clienttoken.spotify.com` (lines 360-416).
4. Download the web player JavaScript (and, if needed, dozens of its chunk
   files) and search the text for the hash of the GraphQL query called
   `fetchPlaylist` (`extractPlaylistHash`, lines 420-506).
5. Call `api-partner.spotify.com/pathfinder/v1/query` with that hash, the
   playlist URI, offset and limit. It pages 343 tracks at a time
   (`fetchPlaylistByID`, 587-623; `queryPartnerAPI`, 626-673).

It extracts only **title, artist names, album name and cover URL**
(`extractTracks`, 677-712). No duration, no ISRC, no Spotify track ID.

**Track limit:** none in principle, because it pages. If a later page fails,
it keeps the partial result (line 616).

**What breaks easily:**
- Any change to the TOTP secret, the token endpoint, the HTML structure, the
  JavaScript bundle names, or the GraphQL hash breaks it. The chunk-finding
  step even relies on "the 4th and 5th JSON-like map in the file"
  (`extractMappings`, 765-773).
- It trusts a secret downloaded at run time from a stranger's server. That is a
  supply-chain risk.
- It breaks Spotify's terms of use (automated access to the service).
- It does reach editorial and algorithmic playlists ("Top 50", "Today's Top
  Hits"), which the official API no longer allows (see `services.md`).

### Apple Music: the public web page is scraped

`apple_music.go`:

- Download the public playlist page (`music.apple.com/{country}/playlist/...`)
  with a browser User-Agent (`fetchAppleMusicPlaylist`, lines 56-103).
- Find the `<script id="serialized-server-data">` JSON blob. Its "sections" hold
  a header (playlist name and artwork) and a track list (title, `artistName`,
  album in `tertiaryLinks[0].title`, artwork template)
  (`extractServerData`, 107-172).
- No developer token, no login.

**Track limit:** whatever Apple server-renders into the page. On 2026-10-21 a
300-track playlist had all 300 in the page; I could not find a larger public
playlist to test. Explo ignores duration (it is in the data) and has no ISRC
(the page doesn't have one).
**Fragility:** the parser assumes a section whose first item has no
`artistName` is the header. Also, if there are several track sections, it keeps
only the last one (it resets the list per section, line 147). Any page redesign
breaks it.

### Custom playlist storage (`custom_playlists.go`)

`CustomPlaylist` (lines 19-30) stores: id (`custom-<random hex>`), name, source
(`listenbrainz` / `apple_music` / `spotify`), source URL, optional LB MBID,
artwork URL, `refresh_days`, colour and `last_fetched`. URL patterns are
checked with regexes (lines 64-88, spotify.go:26-40). Duplicates are detected by
source + source ID (`isDuplicate`, 167-184). The schedule, however, is stored
in `.env` under a key derived from the playlist **name**
(`CUSTOM_<NAME>_SCHEDULE`). Two playlists with the same name therefore share a
schedule (issue #218).

---

## 4. Matching tracks against the music library

Files: `src/client/client.go` (shared scoring), `src/client/subsonic.go`
(Navidrome and other Subsonic servers).

### Searching Navidrome (`Subsonic.SearchSongs`, subsonic.go:128-189)

For each wanted track:

1. Clean the title: strip a trailing "(feat. …)" and "- 2011 Remaster" style
   suffixes (`util.CleanSearchTitle`, `src/util/sanitize.go:17-22`).
2. Call `search3?query=<cleaned title>`. The query holds **only the title**,
   not the artist, and uses the default result count (20 songs in the Subsonic
   API). A very common title can push the right song out of the top 20.
3. If nothing comes back and the track has a recording MBID, search again with
   the MBID as the query (lines 145-160). Navidrome treats a UUID query as an
   exact MBID match (see `services.md`). This fallback was added for issue
   #168, where a user's files had translated titles.
4. Score every result with `BestMatch`.

### The score (`rankResult`, client.go:286-328)

| Check | Points |
|---|---|
| Recording MBID equals the song's `musicBrainzId` (or release-track MBID) | 1000, instant match |
| Normalised title equal | +45 |
| Normalised title contains the other (titles over 3 characters) | +25 |
| Album equal (case-insensitive) | +20 |
| Album contains the other | +15 |
| Main artist equal | +30 |
| Main artist contains the other | +15 |
| File name equals the file name in the song's `path` | +50 |
| Duration within 3 s / 10 s | +10 / +5 |
| Duration differs by more than 30 s | −10 |

"Normalised" means: strip feat./remaster suffixes, lowercase, keep only letters
and digits (`NormalizeTitle`, sanitize.go:25-30). The best result wins if it
reaches **`TRACK_MATCH_SCORE`** (default **60**, `src/config/config.go:65`).
So an exact title + exact artist (75) passes, as does exact title + partial
artist (60).

**Weak spots seen in the issues:**
- Accents and "(Bonus Track)" cause duplicate downloads (#213).
- In the "feat." handling, `Delirium feat. Mark E. Smith` did not equal
  `Delirium (feat. Mark E. Smith)` because the source title used no brackets
  (#163/#164).
- Translated or alternative-script titles never match by title (#168).
- **The +50 file-name bonus almost never fires on Navidrome.** By default
  Navidrome does not report the real file path. It reports a made-up path
  like `Artist/Album/01-10 - Title.flac` (see `services.md`). Explo's own file
  name looks like `Title-Artist.mp3`. Issue #163 shows exactly this mismatch.

### How Explo finds the song IDs after downloading

After downloading, `Client.CreatePlaylist` (client.go:218-244):

1. calls `startScan` (with the optional admin account, since Navidrome only
   lets admins start scans) (`RefreshLibrary`, subsonic.go:191-212);
2. waits 0.5 s, then polls `getScanStatus` every **15 s** until
   `scanning=false` (`CheckRefreshState`, 214-235). If that fails, it sleeps
   `SLEEP` minutes (default 2);
3. runs the same `SearchSongs` again for **all** tracks to get Navidrome song
   IDs;
4. calls `createPlaylist` with every track's ID.

**A hidden bug risk:** Explo reuses one field, `track.ID`, for three different
things: the YouTube video ID or slskd search ID during download, then the
Navidrome song ID. If the search after the scan fails for a downloaded track,
the old YouTube or slskd ID is still in `track.ID` and gets sent to Navidrome
as a `songId`. Releasomat should keep separate fields for "source ID",
"download job ID" and "Navidrome song ID".

---

## 5. Downloading

Files: `src/downloader/*.go`. Downloaders run in the configured order
(`DOWNLOAD_SERVICES`, e.g. `youtube,slskd`). Tracks still missing after one
downloader are tried by the next (`StartDownload`, `downloader.go:64-127`).
Each downloader runs up to 3 tracks at once (`SetLimit(3)`, line 78), with a
rate limiter of one start per second (`DOWNLOAD_LIMITER`, line 80).

### 5.1 YouTube / YouTube Music (`youtube.go`, `youtube_music/search_ytmusic.py`)

**Search** (`QueryTrack`, lines 77-104). The query is `"<title> - <artist>"`.

- **Without** a YouTube API key (the default), Explo runs a tiny Python script
  as a **subprocess**: `python3 search_ytmusic.py "<query>" 10`. The script
  calls `ytmusicapi`'s `YTMusic().search(query, limit=10)` **without a filter**,
  so results mix songs, videos, albums and artists. Explo takes the first
  result that has a `videoId` and whose title has no unwanted keyword
  (`filterYTMusic`, 137-150). There is **no artist check and no duration check**.
- **With** `YOUTUBE_API_KEY`, it uses the official YouTube Data API v3 search
  (`type=video&videoCategoryId=10`, line 86). It prefers videos from an
  "Artist - Topic" channel or a channel named like the artist (`getTopic`,
  171-179). Otherwise it takes the first result without unwanted keywords
  (`gatherVideo`, 286-301).

**Filter list** (`ContainsKeyword`, downloader.go:192-207). The default
`FILTER_LIST` is `live,remix,instrumental,extended,clean,acapella`
(config.go:117). A keyword is ignored if it also appears in the wanted title or
artist, so a wanted "Remix" is still allowed. **For a DJ this default is
harmful:** "Extended Mix" is often exactly what you want. Releasomat needs this
list per playlist.

**Download and format** (`getVideo` 182-196, `saveVideo` 198-283):
- uses `goutubedl` (a Go wrapper that runs the `yt-dlp` program) with format
  `bestaudio`, plus `cookies.txt` if that file exists (lines 61-64);
- writes the stream to `<file>.tmp`, then runs **ffmpeg** to write the final
  file with the extension `TRACK_EXTENSION` (default **mp3**). Converting
  YouTube's Opus or AAC to MP3 **re-encodes lossy audio into lossy audio**,
  which costs quality. The "Error parsing Opus packet header" messages in
  issues #86, #105, #154 and #228 come from this step;
- the file name is `"<title>-<artist>.<ext>"`, made safe by `FilenameSafe`
  (every character that isn't a letter, digit, `.`, `_`, `,` or `-` becomes
  `_`) and cut at 240 bytes without splitting characters (`getFilename`,
  downloader.go:175-189; fix for Japanese titles in #88).

**Tagging** (`src/util/metadata.go`). Tags are written by ffmpeg `-metadata`
options in the same step. Tag names depend on the format: ID3 names for MP3,
Vorbis names for FLAC and Opus (`BuildffmpegMetadata`, 186-203). Explo writes
title, artist(s), album, album artist, date, genre, MusicBrainz IDs (track,
album, release group, artist, album artist, release track), track and disc
numbers, and ISRC.

**Cover art.** Off by default (`EMBED_COVER_ART=false`), because Navidrome
fetches covers itself. When on, Explo downloads the Cover Art Archive (or
Spotify CDN) image into `config/cache/covers/` and embeds it as an attached
picture (saveVideo, 257-268; `util.DownloadCover`, `src/util/http.go`).

**Keeping yt-dlp fresh.** `docker/start.sh` runs `pip install --upgrade yt-dlp`
at every container start (line 8) and adds a nightly cron job for the same
(`YTDLP_UPDATE`, default 23:59, lines 13-14). Many "HTTP 403 Forbidden"
reports (#107, #233) were fixed by updating yt-dlp.

### 5.2 Soulseek through slskd (`slskd.go`, `monitor.go`)

All calls use the header `X-API-Key` against `SLSKD_URL/api/v0/...`.

1. **Search** (`QueryTrack` 137-177, `searchTrack` 198-218). `POST /searches`
   with `{"searchText": "<clean title> - <artist>"}`.
2. **Wait** (`searchStatus`, 220-245). `GET /searches/{id}` every **15 s**, up
   to `SLSKD_RETRY` (5) times. "Complete with 0 files" means no result. Then it
   retries **once** with a wildcard artist: the first letter replaced by `*`,
   keeping a leading "The " (`wildcardArtist`, 456-470). This came from issue
   #141, where exact searches returned 0 results but wildcard searches 250.
3. **Collect candidates** (`CollectFiles`, 273-317) from
   `GET /searches/{id}/responses`:
   - skip users with no free upload slot;
   - work out the extension from the **file name** and ignore the extension
     slskd reports, which can be wrong (issue #202);
   - keep only allowed extensions (`EXTENSIONS`, default `flac,mp3`) and
     names without filtered keywords;
   - skip if the length differs from the wanted duration by more than 10 s;
   - the letters-and-digits-only file path must contain the **title** and
     either the **artist** or the **album**.
4. **Filter and rank** (`filterFiles`, 319-347). Order candidates by the order
   of `EXTENSIONS` (so FLAC first by default) and drop files below
   `MIN_BITRATE` (256) or `MIN_BIT_DEPTH`. Keep up to `SLSKD_DL_ATTEMPTS` (3).
5. **Enqueue** (`queueDownload`, 349-379).
   `POST /transfers/downloads/{username}` with `[{filename, size}]`. The next
   candidate is tried **only if this POST fails**. If the transfer is accepted
   and fails later (rejected, errored, timed out), Explo does not try another
   user. Users ask for this in issue #122.
6. **Clean up.** Delete the search, and remove the finished transfer from
   slskd's list (`deleteDownload`, `Cleanup`, 424-448).

slskd ≥ 0.24 allows only one search request at a time and answers others with
HTTP 429 (issue #79). Explo reacted by limiting concurrency.

### 5.3 Monitoring slskd downloads (`monitor.go:40-158`)

- Every `SLSKD_MONITOR_INTERVAL` (1 min), `GET /transfers/downloads` and match
  each track by username + remote file name (`GetDownloadStatus`,
  slskd.go:382-422). Explo stores the **username in the
  `MainArtistID` field**, another reused field.
- **Success:** the state contains `Succeeded`, or progress is 100 %, or 0 bytes
  remain. Then, if `SLSKD_MIGRATE_DOWNLOADS=true`, move the file from
  `SLSKD_DIR/<remote parent folder name>/<file name>` into the download folder
  (`MoveDownload`, slskd.go:496-514; `parsePath`, 450-454). Explo *assumes*
  slskd's default folder layout here. It can optionally rewrite tags with
  ffmpeg (`SLSKD_OVERWRITE_METADATA`) and apply the path template. Afterwards
  it removes the source folder if it's empty (only that one level, so
  multi-level leftovers stay: issue #180).
- **Give up** if the state is a failure (Aborted, TimedOut, Rejected,
  Errored, Cancelled, normalised in `normalize`, 482-493), if nothing moved
  for `SLSKD_STALL_DURATION` (15 min), or after `SLSKD_MONITOR_MAX_DURATION`
  (120 min). A track missing from the queue on 2 checks in a row is skipped.
- The YouTube downloader also implements this interface but simply reports
  "no monitoring needed".

### 5.4 Lidarr (brief) (`lidarr.go`)

Lidarr downloads **whole albums**. For each track Explo finds the release group
MBID, checks whether Lidarr already has the album (`GET /api/v1/album`), and
either adds it as monitored (`POST /api/v1/album`) or triggers an
`AlbumSearch` command (lines 231-372). It then watches Lidarr's queue and
history, and can move the album's files into the Explo folder using the path
template (`MoveDownload`, 795-852). For a playlist tool this downloads far more
than needed. The maintainer added it in v1.2.0 for users who want full albums
(see the discussion in issue #233).

---

## 6. Creating or updating the playlist in Navidrome

`subsonic.go`:

- **Auth:** the classic Subsonic token method. It makes a random 6-byte salt,
  token = `md5(password + salt)`, and sends `u`, `t`, `s`, `v=1.16.1`, `c=explo`
  on every request (`GetAuth`, 101-118; `subsonicRequest`, 300-315). This
  needs the **plain password** stored in config.
- **Replace mode (default on).** Before the run, find the old playlist by
  **name** (`getPlaylists`) and **delete** it (`SearchPlaylist` 259-280,
  `DeletePlaylist` 291-298). After downloading, create a brand-new playlist
  with `createPlaylist?name=...&songId=...&songId=...` (237-257). Then call
  `updatePlaylist` to set the comment ("Created for X by Explo …") and the
  public flag (282-289).
- **Consequences:**
  - the playlist gets a **new ID every run**, so apps that sync playlists
    (Symfonium and others) see a new playlist each time (issue #210);
  - if the run crashes between delete and create, the playlist is simply gone;
  - all song IDs go into **one GET URL**, which can get very long for big
    playlists;
  - with replace off, names get a date suffix (`Weekly-Exploration-2026-Week43`,
    `getPlaylistName`, config.go:349-386), and a new playlist appears every
    run.
- Users who saw duplicate playlists on back-to-back runs (#90, #181) had
  replace switched off, so every run made a new dated playlist, or had runs
  overlapping (for example `EXECUTE_ON_START` plus a scheduled run).

Better options exist in the Subsonic API (see `services.md`).
`createPlaylist?playlistId=<id>&songId=...` replaces the songs of an existing
playlist and keeps its ID. Navidrome also accepts form POST bodies.

---

## 7. Scheduling

There are **two** schedulers. It's easy to get confused about which one does
what.

### 7.1 System cron, set up by `docker/start.sh` (runs the real work)

At container start (`start.sh`):

1. Empty the crontab (`crontab -r`, line 10). This fixed duplicated cron lines
   after restarts in v1.2.0 (issue #222).
2. Add the nightly yt-dlp update (lines 13-16).
3. Start the web UI in the background (line 41).
4. Read every `*_SCHEDULE` and `*_FLAGS` line from the `.env` file, unless
   already set as real environment variables (lines 48-61).
5. For every `X_SCHEDULE` variable, add a cron line that runs
   `./explo --config .env $X_FLAGS` on that schedule (lines 73-91). Examples:
   - `WEEKLY_EXPLORATION_SCHEDULE=15 00 * * 2`, flags `--playlist weekly-exploration`
   - `WEEKLY_JAMS_SCHEDULE=30 00 * * 1`
   - `DAILY_JAMS_SCHEDULE=15 01 * * *`
   - `ON_REPEAT_SCHEDULE=0 12 1 * *`
   - `CUSTOM_<NAME>_SCHEDULE=0 4 * * *`, flags `--playlist custom-<id>`

   (Built-in defaults from `src/web/backend/defs/defs.go:178-183`; the
   custom-playlist default from `playlist/handlers.go:162-169`.)
6. Optionally run once now (`EXECUTE_ON_START`, lines 97-101), then run
   `crond` in the foreground.

**Consequences:**
- The UI writes schedule changes into `.env`, but cron reads `.env` only at
  container start. Changes need a **container restart**. The UI says so
  (`Settings.jsx:395`).
- Each scheduled run is a separate process. Nothing stops two runs from
  overlapping, such as a cron run and a manual UI run. Only manual runs are
  locked against each other (`run/manual_run.go:144-218`).
- One schedule per playlist; users can't add a second time slot for a retry
  (issue #233).
- Flags such as `--replace-playlist=false` and `--clean-downloads` are
  per-playlist settings, stored as text appended to the `*_FLAGS` line. The UI
  edits them with string search and replace
  (`settings/handlers.go:99-170, 217-316`).

### 7.2 gocron inside the web UI process (housekeeping only)

`src/web/backend/server.go:147-160` and `jobs/jobs.go`:

- the cover-cache cleanup, daily at 03:00, trims `config/cache/covers` to
  `WEB_CACHE_MB` (`RegisterCoverCleanup`, jobs.go:39-50);
- **custom playlist refresh** (`RegisterCustomPlaylistRefresh`,
  `playlist/jobs.go:15-71`). For each imported playlist that has flags, add a
  gocron job on the same `CUSTOM_<NAME>_SCHEDULE` (default `0 4 * * *`). It
  re-fetches the source (Spotify, Apple or LB) and rewrites the cached track
  list, but only if `refresh_days` have passed since `last_fetched`
  (line 45).

**Consequences:**
- The full run (cron) and the cache refresh (gocron) for a custom playlist fire
  **at the same minute**. The run reads the cache straight away, while the
  refresh needs several seconds to fetch. So the run most likely works with
  the *previous* track list, a one-cycle lag. This is my reading of the code;
  no issue reports it.
- The `refresh_days` check compares against the `last_fetched` value captured
  when the job was registered. After the first refresh that value is stale,
  so the check only helps until the next container restart.
- With the default daily schedule, the full run (search, download, rebuild the
  playlist) happens every day, even when `refresh_days` is 7.

---

## 8. File lifecycle: does Explo delete old downloads?

**Short answer: only in two narrow cases. It never decides per track, and it
keeps no record of what it downloaded.**

### 8.1 Where files go

- `DOWNLOAD_DIR` defaults to `/data/` (`config.go:61, 97`). With
  `USE_SUBDIRECTORY=true` (default), every run downloads into
  `DOWNLOAD_DIR/<PlaylistName>/` (`GenPlaylistDetails`, config.go:341-346).
- `<PlaylistName>` depends on the replace setting (`getPlaylistName`,
  config.go:349-386):
  - **replace on (default):** a fixed name, e.g. `Weekly-Exploration` or
    `Custom-1a2b3c`, so the **same folder** every run;
  - **replace off:** a dated name, e.g. `Weekly-Exploration-2026-Week43` or
    `Daily-Jams-2026-Day294`, so a **new folder** every run.
- A `PATH_TEMPLATE` such as `{{Artist}}/{{Album}}/{{TrackName}}.{{ext}}`
  creates subfolders **inside** that playlist folder.
- slskd files without `SLSKD_MIGRATE_DOWNLOADS`, and Lidarr files without
  `LIDARR_MIGRATE_DOWNLOADS`, stay in slskd's or Lidarr's own folders. Explo
  never touches them again.

### 8.2 Case 1: `--clean-downloads` at the start of a run

- The flag is defined in `src/config/flags.go:39`, default **off**. The UI
  toggle "Clean old downloads" ("Remove previously downloaded tracks before
  each run") adds the flag to the four **built-in** playlists' `*_FLAGS` only,
  not to imported playlists (`HandleSaveCleanDownloads`,
  `settings/handlers.go:274-316`, loop at line 293).
- **When:** right at the start of the run, *before* the library check and
  *before* downloading (`main.go:227-229`).
- **Condition:** only if `USE_SUBDIRECTORY=true` (main.go:227; warning in
  config.go:296-298).
- **What it deletes** (`DownloadClient.DeleteSongs`, downloader.go:140-154):
  every **regular file directly inside** `DOWNLOAD_DIR/<PlaylistName>/`. It
  skips subdirectories and does not recurse. It does not check whether the
  track is still in the new playlist.

**What that means in practice:**
- **Replace on:** last run's files in the fixed folder are deleted, including
  tracks that are *also* in this week's playlist. Navidrome hasn't rescanned
  yet, so the library check still finds those songs, marks them "present"
  and does not download them again. The playlist then points at songs whose
  files are gone. Issue **#109** (open) reports exactly this.
- **Replace off:** the playlist name is new each run, so `DeleteSongs` looks in
  the *new*, still non-existent folder and deletes **nothing**. Old weekly
  folders stay forever, and so do the old dated playlists.
- **With a path template that makes subfolders:** the files sit in
  subfolders, which `DeleteSongs` skips, so **nothing** is deleted.
- slskd or Lidarr files that were not moved are never deleted.

### 8.3 Case 2: deleting an imported playlist in the UI

- The delete dialog has a checkbox "Also delete downloaded files"
  (`PlaylistCard.jsx:739-745`), which sends `?delete_tracks=true`.
- `HandleDeleteCustomPlaylist` (`playlist/handlers.go:253-319`) removes the
  metadata, the cache file and the `.env` schedule keys. If the box was ticked,
  it reads `USE_SUBDIRECTORY` and `DOWNLOAD_DIR` **from the `.env` file**
  (lines 297-299). If those are `"true"` and non-empty, it calls
  `util.RemoveDirsByPrefix(DOWNLOAD_DIR, "Custom-<id>")`
  (`src/util/filesystem.go:13-31`). That **recursively deletes** every folder
  directly under `DOWNLOAD_DIR` whose name starts with `Custom-<id>`, then
  starts a background library rescan.
- Explo's Docker guidance tells users to leave `DOWNLOAD_DIR` out of `.env`
  (issue #228), so `env["DOWNLOAD_DIR"]` is empty. In that recommended setup
  the deletion appears to be **skipped silently** (it only logs "skipping
  track cleanup").
- The playlist inside Navidrome is **not** deleted.

### 8.4 What is never deleted

- Old dated playlists in Navidrome (replace off).
- Files of tracks that dropped out of a playlist, unless case 1 or 2 applies.
- Cover art is trimmed by size (section 7.2), but that is cache, not music.

### 8.5 Lesson for Releasomat

Keep a **database of every file Releasomat downloads**: path, which
playlist(s) use it, when it was last in a playlist. Delete only files that are
in no current playlist and older than a grace period. Do the deletions
**after** the new playlist is built and Navidrome has rescanned, never before
the library check. Never delete files Releasomat didn't download.

---

## 9. Path templates and sanitising

**Template variables** (`buildTrackPath`, downloader.go:242-279):
`{{Artist}}` (main artist), `{{Album}}` / `{{AlbumName}}`, `{{TrackName}}`
(clean title), `{{TrackNumber}}` and `{{DiscNumber}}` (two digits),
`{{Year}}`, `{{File}}` (current file name) and `{{ext}}`. Without enrichment,
track number and year are often `00` and `0`.

**Sanitising each value** (`sanitize`, downloader.go:217-240):
- trim spaces;
- replace `/`, `\` and `:` with `-`; remove `* ? " < > |`;
- turn `.`, `..` or empty into `_`.

**Safety checks on the whole path:**
- `filepath.Clean`, then `filepath.IsLocal` rejects absolute paths and `..`
  escapes (lines 264-268). That is a good guard;
- if the result has no extension, the original extension is added
  (lines 270-275);
- the template is applied relative to the playlist folder
  (`DOWNLOAD_DIR/<PlaylistName>`).

**Default file names** use the stricter `util.FilenameSafe` (anything that isn't
a Unicode letter, digit, `.`, `_`, `,` or `-` becomes `_`,
`src/util/sanitize.go:9, 32-34`). `getFilename` cuts names to 240 bytes without
breaking multi-byte characters.

**Presets** (`src/web/backend/settings/path_templates.go:16-39`) are just a
list of `{name, template}` saved in `config/path-templates.json` for the UI.
The active template is the single `PATH_TEMPLATE` line in `.env`, one for all
playlists and all downloaders.

**Gaps:** no length limit per path segment (some filesystems allow 255
bytes); no handling of a trailing dot or space; no Windows reserved names (not
an issue on Unraid); no check for two tracks producing the same path (the
second simply overwrites the first).

---

## 10. What Explo users struggle with

From the last 100 issues (#72–#240, Oct 2025 – Oct 2026), most-commented
first.

### Matching quality
- Duplicates because of accents, "(Bonus Track)", translated titles,
  "feat." written differently (#213, #168, #163, #164).
- Users ask Explo to **check the downloaded file** and try another candidate
  if it's the wrong song (#163, open).
- Jellyfin and Plex search quirks: songs not found, or searches across the
  wrong libraries (#85, #33, #131).
- Overlapping tracks between Weekly Exploration and Weekly Jams are a
  ListenBrainz matter, not Explo's (#238).

### YouTube blocking and cookies
- "HTTP Error 403: Forbidden" and "Sign in to confirm you're not a bot" (#78,
  #107, #233). The fixes: update yt-dlp, wait, space out requests, and pass a
  `cookies.txt` (added in v0.11.3).
- The "Opus packet header" log noise from ffmpeg (#86, #105, #228).
- Users want a choice of download order, YouTube first or Soulseek first
  (#221).

### slskd problems
- Failed transfers are not retried with another user (#122, open, 9
  comments). Users report 15 of 50 songs downloaded.
- Exact searches find nothing; wildcards help (#141).
- The extension filter took wrong files (#202, fixed); the bitrate limit was
  off by one (fixed).
- slskd's one-search-at-a-time limit caused 429 errors (#79).
- Moving files: wrong mount paths, empty folders left behind, files "lost"
  without a clear error (#151, #162, #180, #197).
- One report of the home router crashing during slskd searches (#114).

### Navidrome / Subsonic specifics
- Songs not found after download: bad file names for CJK titles (#88, fixed),
  special characters such as `:` breaking search (#163).
- Duplicate playlists on repeated runs (#90, #181); playlist re-created with a
  new ID breaks sync in apps like Symfonium (#210, open).
- Subsonic auth failed when the password contained `$` followed by certain
  characters (#144). That is a `.env` parsing pitfall.
- Starting a scan needs an admin account (handled with `ADMIN_SYSTEM_*`).

### Scheduling
- Cron jobs failing because the run tried to start the web server too (#176).
- Duplicated cron entries after restarts (#222, fixed in v1.2.0).
- Schedule changes need a container restart.
- Only one time slot per playlist (#233, #179).
- ListenBrainz downtime and timeouts make weekly runs fail (#89, #108, #138,
  #102). The MetaBrainz blog post of 2026-08-19 confirms capacity problems.

### Setup, permissions and UI
- Docker path confusion: `DOWNLOAD_DIR` vs volume mounts (#228, #154).
- Files owned by root; PUID/PGID support only arrived later (#103, #209, #165).
- Config breakage on upgrade: the `.env` path treated as a directory (#139),
  custom playlists lost or duplicated (#184).
- UI toggles not working ("Save each run as new playlist", #233); the UI
  showing stale playlists (#177).
- Feature requests: other download sources (SpotiFLAC, tidal-dl,
  octo-fiesta: #117, #118, #95), full albums (#206, #236), keeping the
  playlist cover (#232), ReplayGain (#240).

---

## 11. Adopt or do differently: a checklist for Releasomat

### Adopt (good ideas from Explo)
1. **Use ListenBrainz's `source_patch`** to identify Weekly Exploration,
   Weekly Jams and Daily Jams, and page through `createdfor`.
2. **Use the recording MBID first** when matching. Navidrome's `search3`
   accepts an MBID as the query.
3. **A scored matcher** using title, artist, album and duration, with a
   tunable threshold and normalised strings (feat. and remaster suffixes
   removed).
4. **A downloader chain** (try YouTube, then Soulseek, or the other way
   round), configurable per playlist.
5. **A keyword filter** that is skipped when the keyword is part of the wanted
   title.
6. **slskd candidate rules:** must have a free upload slot, take the extension
   from the file name, duration within ±10 s, file path must contain the title
   plus the artist or album, prefer formats in order, minimum bitrate and bit
   depth. Also the wildcard retry.
7. **Stall and maximum-time limits** when watching slskd transfers.
8. **Path template safety:** sanitise each part, then reject anything that
   isn't a relative path inside the target folder.
9. **Tag downloaded files** with MusicBrainz IDs and ISRC whenever known. This
   makes later matching exact.
10. **Keep yt-dlp up to date** automatically. Its YouTube support breaks every
    few weeks without updates.

### Do differently
1. **One long-running Python process with its own scheduler** (for example
   APScheduler) instead of cron + CLI + `.env` rewriting. Schedule changes
   apply immediately, and the app can prevent overlapping runs of the same
   playlist.
2. **A real database** (SQLite) for playlists, tracks, matches, download jobs,
   files on disk, and run history. Configuration shouldn't live as text
   appended to `*_FLAGS` lines.
3. **Track every file Releasomat writes**, and delete only what no playlist
   needs any more, *after* the new playlist exists and Navidrome has
   rescanned (section 8.5).
4. **Update the Navidrome playlist in place**: keep its ID, replace its songs
   with `createPlaylist?playlistId=…` (sent as a form POST). Look the playlist
   up by the stored ID, not by name.
5. **Separate ID fields:** source track ID, downloader job ID, Navidrome song
   ID. Never reuse fields such as `MainArtistID` for a username.
6. **Find downloaded songs by something exact.** Search by MBID if the file
   has one; otherwise use Navidrome's native API `path` filter, or a targeted
   scan and then search. Don't rely on the Subsonic `path` field: by default
   it's a fake path (see `services.md`).
7. **Verify after download.** Read the tags and duration of the downloaded
   file and compare them with the wanted track. On a mismatch or a failed
   transfer, try the next candidate or the next user (users asked for this in
   #122 and #163).
8. **Search YouTube Music with `filter="songs"`** and check artist and
   duration. Don't take the first mixed result.
9. **Don't re-encode by default.** Keep YouTube's original Opus or M4A (or
   remux it). Offer MP3 only as an explicit choice.
10. **Call yt-dlp and ytmusicapi as Python libraries** in-process. No
    subprocess or temp-file round trips, and real progress reporting.
11. **Spotify:** avoid a runtime dependency on secrets fetched from third-party
    servers. See `services.md` for safer choices, and make any keyless
    scraping an opt-in "may break" feature.
12. **Retry ListenBrainz with backoff in the background** (and mark the run
    "waiting for ListenBrainz"), instead of blocking a run for 25 minutes.
13. **A per-playlist keyword filter.** "Extended Mix" and "Remix" are wanted
    for a DJ.
14. **Store secrets carefully.** Subsonic token auth needs the plain password.
    Keep it in a separate secrets file or environment variable, not in a file
    the UI can show.

---

## Sources

- Explo repository, tag v1.2.0 (commit `51449865c526c88f05b246b4d2447efc204854d2`):
  https://github.com/LumePart/Explo/tree/v1.2.0
- Explo issues (last 100, read 2026-10-21): https://github.com/LumePart/Explo/issues?q=is%3Aissue
  - #79 https://github.com/LumePart/Explo/issues/79
  - #88 https://github.com/LumePart/Explo/issues/88
  - #89 https://github.com/LumePart/Explo/issues/89
  - #92 https://github.com/LumePart/Explo/issues/92
  - #102 https://github.com/LumePart/Explo/issues/102
  - #108 https://github.com/LumePart/Explo/issues/108
  - #109 https://github.com/LumePart/Explo/issues/109
  - #122 https://github.com/LumePart/Explo/issues/122
  - #141 https://github.com/LumePart/Explo/issues/141
  - #151 https://github.com/LumePart/Explo/issues/151
  - #163 https://github.com/LumePart/Explo/issues/163
  - #164 https://github.com/LumePart/Explo/issues/164
  - #168 https://github.com/LumePart/Explo/issues/168
  - #176 https://github.com/LumePart/Explo/issues/176
  - #180 https://github.com/LumePart/Explo/issues/180
  - #181 https://github.com/LumePart/Explo/issues/181
  - #202 https://github.com/LumePart/Explo/issues/202
  - #210 https://github.com/LumePart/Explo/issues/210
  - #213 https://github.com/LumePart/Explo/issues/213
  - #218 https://github.com/LumePart/Explo/issues/218
  - #222 https://github.com/LumePart/Explo/issues/222
  - #228 https://github.com/LumePart/Explo/issues/228
  - #233 https://github.com/LumePart/Explo/issues/233
  - #238 https://github.com/LumePart/Explo/issues/238
- MetaBrainz blog, "Growing pains: an update on the ListenBrainz service status" (2026-08-19):
  https://blog.metabrainz.org/2026/08/19/growing-pains-an-update-on-the-listenbrainz-service-status/
- Navidrome Subsonic path reporting (`fakePath`, `ReportRealPath`), v0.64.2:
  https://github.com/navidrome/navidrome/blob/v0.64.2/server/subsonic/helpers.go
