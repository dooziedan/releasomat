# Music services and tools: current state for Releasomat

*Research notes, checked on 2026-10-21. Each claim says where it comes from and
when I found it. Things marked "tested" I checked myself on 2026-10-21 with a
simple read-only request. Things marked "unverified" are reasonable guesses
that someone should confirm before building on them.*

This file covers:

- **Playlist sources:** where Releasomat reads playlists.
- **Back-end tools:** slskd, Navidrome, yt-dlp and ytmusicapi.
- **Matching:** how to recognise the same song across services.

## What Releasomat does now (2026-10-22)

Decisions after this research: no Spotify; all other sources below are built in, equal and always on
(ADR 0006). Findings while building them, which this file's older text doesn't cover:
- **Apple Music**: the page JSON held all 300 of 300 tracks in a live check; a warning appears in the
  run if the page announces more tracks than it contains.
- **Deezer**: the playlist answer lists at most 400 tracks inline; longer playlists are read through
  `/playlist/<id>/tracks` in pages (1002-track playlist: all read, 944 with ISRC).
- **SoundCloud**: a flat yt-dlp list has no titles, so every track is read on its own (about 20 s for
  100 tracks).
- **Beatport**: share links (`/playlists/share/…`) load their tracks only in the browser with an
  anonymous login token, so Releasomat says it can't read them. Charts and Top 100 / Hype pages work
  (first 100 tracks, with ISRC, BPM and key).
- **Tidal**: built from the official OpenAPI description; not yet tested with real credentials.

## Glossary (plain language)

- **API**: a web address a program calls to get data as JSON instead of a web
  page.
- **OAuth**: the "log in with X" flow. *Client credentials* means the app logs
  in as itself, with no user. *Authorization code* means a real user logs in
  and gives the app permission.
- **Scraping**: reading data out of a web page meant for browsers. It works
  until the site changes, and it usually breaks the site's terms of service
  (ToS).
- **ISRC**: International Standard Recording Code, a 12-character ID for one
  recording (e.g. `USUG11902877`). It's the best cross-service key when
  present.
- **MBID**: MusicBrainz ID, a UUID. A *recording MBID* identifies one
  recording. ListenBrainz uses these.
- **JSPF**: a JSON playlist format; ListenBrainz uses it.

---

## Summary table

| Source | How Releasomat reads a playlist | Auth needed | ISRC? | Risk | Recommendation |
|---|---|---|---|---|---|
| **ListenBrainz** | Official API (JSPF) | None for public; token optional | No (has MBIDs, better) | Low (service often slow in 2026) | **Core feature** |
| **Deezer** | Official public API | None | **Yes** | Low | Easy first import source |
| **Tidal** | Official API v2, client credentials | Developer app (client ID + secret) | **Yes** (+ BPM, key) | Low | Good official source |
| **YouTube Music / YouTube** | ytmusicapi / yt-dlp (unofficial) | None for public | No | Medium | Supported, best effort |
| **SoundCloud** | yt-dlp (unofficial) | None | Rarely | Medium | Supported, best effort |
| **Apple Music** | Public page JSON (scrape); optional web-player token for ISRC | None (official API needs paid Apple Developer account) | Only via web-player token or official API | Medium–High | Supported, "may break" label |
| **Spotify (own playlists)** | Official Web API, user OAuth | Each user's own Spotify app + **Premium** | Yes | Low–Medium | Supported for playlists the user owns or collaborates on |
| **Spotify (any public link)** | Embed page (scrape), max **100** tracks | None | No | High (ToS + breakage) | Optional, clearly labelled |
| **Beatport** | Public page JSON (`__NEXT_DATA__`) | None | **Yes** (+ BPM, key, mix name) | Medium–High | Worth it for the DJ use case; opt-in |
| **Qobuz** | Partner-only API | Partner app_id | — | High | Skip for now |
| Amazon Music | No public API | — | — | High | Skip |

---

## Spotify

### What changed (official Web API)

**November 27, 2024** (Spotify developer blog). New apps, and development-mode
apps without a pending extension request, lost access to *algorithmic and
Spotify-owned editorial playlists*. Examples: Discover Weekly, Today's Top
Hits, "Top 50" charts. They also lost Related Artists, Recommendations, Audio
Features, Audio Analysis, Featured and Category playlists, and preview URLs in
multi-get responses.

**May 15, 2025.** Extended quota (the only way out of development mode) is
reserved for "established, scalable, and impactful use cases". The quota
modes page says: organisations only, a legally registered business, at least
**250k monthly active users**, available in key markets. A self-hosted app
will never qualify.

**February 2026** (blog post 2026-02-06 and the migration guide). New
development-mode apps were affected from Feb 11, existing ones from **March 9,
2026**:
- the app owner must have **Spotify Premium**, otherwise the app stops working;
- at most **5 authorised users** per app, each added to the app's allowlist
  (it used to be 25);
- `/playlists/{id}/tracks` was renamed to **`/playlists/{id}/items`**;
- **playlist contents are only returned for playlists the current user owns
  or collaborates on.** For any other playlist the `items` field is missing,
  and the items endpoint returns **403**;
- the batch endpoints (`GET /tracks`, `/albums`, `/artists`…) were removed,
  as were `GET /users/{id}/playlists`, browse categories, new releases and
  artist top tracks;
- the search `limit` maximum dropped to 10;
- the track fields `popularity`, `available_markets` and `linked_from` were
  removed. `external_ids` (which holds the **ISRC**) was removed too, but the
  **March 2026 changelog reverted that**, so ISRCs are available again.

The current reference page for "Get Playlist Items" says, verbatim: the
endpoint "is only accessible for playlists owned by the current user or
playlists the user is a collaborator of". Page size: max **50**, default 20.

**July 23, 2026.** Up to 25 Client IDs per developer account (was 1). Quota is
now counted per developer *account*. A quota 429 response carries
`"reason": "QUOTA_EXCEEDED"`. No change to the playlist rule or the 5-user
cap was announced (checked the May and July 2026 changelogs).

**Client credentials vs user OAuth.** Client credentials has no "current
user", so it can't own or collaborate on anything. Under the 2026 rules it
therefore can't read the tracks of *any* playlist (my inference from the
rules above). **User OAuth (Authorization Code with PKCE)** can read the
user's *own* and *collaborative* playlists.

### Keyless alternatives

**1. The embed page** (`https://open.spotify.com/embed/playlist/{id}`).
*Tested 2026-10-21:* the HTML contains a `<script id="__NEXT_DATA__">` JSON
blob. At `props.pageProps.state.data.entity.trackList` it holds **at most 100
tracks**: a 150-track editorial playlist ("All Out 80s") returned exactly
100. Each entry has `uri` (Spotify track ID), `title`, `subtitle` (artist
names joined), `duration` (ms), `isExplicit` and a preview URL. It has **no
album and no ISRC**. Editorial playlists work. The same JSON also holds an
**anonymous access token** (`settings.session.accessToken`,
`isAnonymous: true`).

**2. The web player's internal GraphQL ("pathfinder") API.** This is what
Explo does (see `explo-reference.md` §3). Libraries such as SpotifyScraper
bootstrap the anonymous token from the embed page and then call pathfinder
with "persisted query" hashes to page past 100 tracks. It works today, but it
breaks whenever Spotify changes its web player (hashes, TOTP secrets), and it
clearly breaks Spotify's terms, which forbid automated or scraped access.

**3. ListenBrainz import.** The ListenBrainz API documents
`GET /1/playlist/spotify/{playlist_id}/tracks` ("Import a playlist's tracks
from Spotify and convert them to JSPF"). It needs a ListenBrainz token,
presumably with Spotify linked in the user's ListenBrainz settings.
*Unverified:* whether it can read playlists the user doesn't own under the
2026 rules. Worth a test, because the result comes back already
MBID-friendly.

### Recommendation for Releasomat

- **"Spotify (my playlists)", official, risk low–medium.** Each Releasomat
  user creates their *own* Spotify developer app (Client ID; redirect URL to
  Releasomat), logs in once with PKCE, and Releasomat stores the refresh
  token. That works for playlists the user owns or collaborates on. To follow
  someone else's public playlist, the user can copy it into their own account
  first. Needs Premium (development-mode rule). Gives ISRCs.
- **"Spotify (public link)", keyless via the embed page, risk high.** Make it
  opt-in and label it "first 100 tracks, may stop working". It's useful for
  editorial and chart playlists.
- **Do not** build the TOTP/pathfinder approach into the core, and never
  fetch secrets from third-party servers at run time. If it's added at all,
  make it a separate, clearly experimental plugin.
- Spotify data lacks MBIDs. Match by ISRC when available (official route),
  otherwise by title, artist and duration.

---

## Apple Music

### Official route

The Apple Music API (`api.music.apple.com/v1/catalog/{storefront}/playlists/{id}`
plus the `tracks` relationship, paged with `next`) needs a **developer token**.
That is a JWT signed with a MusicKit key, which requires a paid **Apple
Developer Program** membership (99 USD/year; Apple developer forum threads).
Catalog requests then need no user login. Song attributes include **`isrc`**.

### Keyless route 1: the public page (what Explo does)

`https://music.apple.com/{country}/playlist/{slug}/{pl.id}` contains
`<script id="serialized-server-data">` JSON.
*Tested 2026-10-21:*
- a 50-track and a 300-track playlist were fully present (sections
  `playlist-detail-header-section`, `track-list`, `track-list-footer-section`);
- each track has `title`, `artistName`, `duration` (ms), the album in
  `tertiaryLinks[0].title`, and the Apple song ID in
  `contentDescriptor.identifiers.storeAdamID`;
- **no ISRC**;
- I couldn't find a public playlist over 300 tracks to test a cap. Third-party
  reports say the web player loads tracks in pages of 100 or 300 (Apple
  community forums).

### Keyless route 2: the web player's token

*Tested 2026-10-21:* Apple's web player JavaScript bundle
(`music.apple.com/assets/index~….js`) contains a developer JWT (issuer
`AMPWebPlay`, valid about 10 weeks, allowed origin `apple.com`). Two things
make it work:
- the header `Origin: https://music.apple.com`;
- the host `amp-api.music.apple.com`.

With both, catalog calls work: playlist tracks in pages of 100 with a `next`
link, including **`isrc`**, `albumName` and `durationInMillis`. The token
rotates. A developer log reports that Apple moved the bundle after WWDC 2026,
which broke tools that look for it. Using Apple's own token outside its player
is against Apple's terms.

### Recommendation

- Default: **page JSON**, risk medium (layout changes break it).
- Optional "enrich with ISRC" via the web-player token, risk medium–high.
  Must fail gracefully back to page JSON.
- If the user has an Apple Developer account, allow an **official developer
  token**, risk low.
- Playlist IDs: `pl.` followed by hex for editorial playlists, `pl.u-…` for
  user playlists shared publicly.

---

## Deezer

*Tested 2026-10-21, no key:*
- `GET https://api.deezer.com/playlist/{id}` returns playlist info plus
  `tracks.data` (a 180-track playlist came back complete, `nb_tracks` 180);
- `GET https://api.deezer.com/playlist/{id}/tracks?index=0&limit=100` pages
  through tracks (default page 25, with a `next` URL);
- every track has `id`, `title`, `title_short`, `title_version`, `duration`
  (seconds), `artist.name`, `album.title` and **`isrc`**.

**Rate limit:** about **50 requests per 5 seconds per IP**. That's
community-reported (Rocksky's Deezer service); I didn't find an official page.
**Auth:** none for public playlists; private ones need OAuth (not needed).
**Risk:** low. It's a long-standing public API, with no sign of change in
2026.

**Recommendation:** implement early. It's the cleanest source, with ISRC on
every track.

---

## Tidal

**TIDAL API v2** at `https://openapi.tidal.com/v2`, JSON:API format.
*Checked 2026-10-21 in the official OpenAPI file* (`tidal-api-oas.json` in
`tidal-music/tidal-sdk-web`, API version 1.10.169):
- `GET /playlists/{id}` and `GET /playlists/{id}/relationships/items` allow
  **Client Credentials**, so no user login is needed;
- items are paged with a `page[cursor]` parameter, plus `countryCode` and
  `include=items` (to get the track attributes in the same response);
- track attributes: `title`, `version`, **`isrc`**, `duration` (ISO 8601,
  e.g. `PT3M27S`), **`bpm`**, **`key`**, `keyScale`, `explicit`;
- playlist attributes include `accessType` (`PUBLIC` / `UNLISTED` /
  `PRIVATE`) and `numberOfItems`;
- token URL for client credentials: `https://auth.tidal.com/v1/oauth2/token`.
- **Rate limits:** the spec only shows a 429 "Rate limit exceeded" response;
  I found no published numbers.
- **Setup:** register an app on developer.tidal.com to get a client ID and
  secret. *Unverified:* whether a paid TIDAL subscription is needed to
  register. The portal page is JavaScript-only and I couldn't read it.

**Recommendation:** official and stable, risk low. A good second official
source after Deezer. BPM and key are a bonus for DJs.

---

## YouTube Music and YouTube

**Reading playlists:**
- **ytmusicapi** (v1.12.3, 2026-09-16, MIT): `YTMusic().get_playlist(playlistId, limit=None)`
  returns all tracks (`limit=None` means "retrieve them all"). Each track has
  `videoId`, `title`, `artists`, `album`, `duration_seconds` and
  `isAvailable`. Public playlists work without login; private ones need
  browser-header or OAuth auth (ytmusicapi docs).
- **yt-dlp** (as a library): `extract_info(url, download=False)` with
  `extract_flat='in_playlist'` lists the entries of YouTube and YouTube Music
  playlists quickly (ID, title, duration where available).
- **No ISRC.** Titles on plain YouTube are messy ("Artist - Title (Official
  Video)").

**Risks:** both tools are unofficial and reverse-engineered, so they break
from time to time and need updates. YouTube's terms forbid automated access.
**Recommendation:** ytmusicapi for `music.youtube.com` links, yt-dlp flat
extraction for `youtube.com` links. Risk medium.

---

## SoundCloud

- **yt-dlp** reads public sets (playlists) and tracks without an API key. It
  finds a client ID from SoundCloud's web app by itself.
- **The official API** is self-serve again, but registration requires an
  **Artist Pro** subscription (SoundCloud developer blog, 2026). With client
  credentials you can read public playlists. Token limits: 50 per 12 h per
  app and 30 per hour per IP (official docs).
- Metadata quality: titles are often "Artist - Title" uploaded by a label or a
  user account, so they need parsing. Go+-only tracks may be preview-only. That
  doesn't matter much, because Releasomat only *reads* the list here and
  downloads elsewhere.

**Recommendation:** yt-dlp flat extraction, risk medium.

---

## Qobuz (brief)

The official API is **partner-only**: you need an `app_id` and secret from
Qobuz (API directories; no public sign-up). Community tools use the private
web-player API with an app ID taken from the web player. **Skip for now**:
risk high, few users.

## Beatport (brief, but important for a DJ)

- **The official API v4** (`api.beatport.com/v4`) is gated to approved
  partners. The community plugin `beets-beatport4` works around this by
  reusing the client ID of Beatport's own API-docs page. That's a terms grey
  zone.
- **Public pages** are Next.js sites with a `__NEXT_DATA__` JSON blob.
  *Tested 2026-10-21* on `https://www.beatport.com/top-100`: 100 tracks with
  `name`, **`mix_name`** (e.g. "Extended"), `artists`, `remixers`, **`isrc`**,
  **`bpm`**, **`key`** (e.g. "Gb Minor"), `length_ms`, genre, label and
  release. Charts (`/chart/<slug>/<id>`) and genre Top 100 pages are public
  the same way. Users' own Beatport playlists live behind login
  (*unverified*).
- **DJ notes:**
  - the search query must include the **mix name** ("Original Mix",
    "Extended Mix");
  - YouTube often has only the radio edit, so Soulseek is the realistic
    source for extended mixes;
  - Explo's default keyword filter (`extended`, `remix`) would throw away
    exactly these tracks.

**Recommendation:** an opt-in "Beatport chart / Top 100 by URL" source that
reads `__NEXT_DATA__`. Risk medium–high (page structure, terms), value high
for this user.

## Others

- **Amazon Music:** no public playlist API. Skip.
- **Bandcamp:** no playlists; yt-dlp handles albums and tracks if ever needed.
- **Odesli / song.link API** (cross-service link lookup): the
  `v1-alpha.1` namespace was announced for retirement on 2026-07-31, and new
  API keys are no longer issued (Songlink notice).
  *Tested 2026-10-21:* `api.song.link/v1-alpha.1/links` returns **401** without
  a key. Don't rely on it.

---

## ListenBrainz

### Endpoints (official docs, checked 2026-10-21)

Base: `https://api.listenbrainz.org/1/`

| Endpoint | Auth | Notes |
|---|---|---|
| `GET user/{user}/playlists/createdfor?count=&offset=` | none ("these playlists are all public") | The "Created for you" list. Default page size is 25 (seen in Explo issue #92), so page through all. |
| `GET user/{user}/playlists?count=&offset=` | optional token | User's own playlists; with token also private ones. |
| `GET user/{user}/playlists/collaborator` | optional | Playlists the user collaborates on. |
| `GET playlist/{playlist_mbid}?fetch_metadata=true` | none for public | Full playlist in **JSPF**. |
| `GET playlist/{playlist_mbid}/xspf` | same | XML version. |
| `GET metadata/recording/?recording_mbids=a,b,c&inc=artist release tag` | not stated | Batch metadata for MBIDs. |
| `GET/POST metadata/lookup/` | **token required** ("because of possible abuse by AI scrapers") | Artist + recording name → recording MBID and release MBID. Useful to give Spotify/Apple tracks an MBID. |
| `GET playlist/spotify/{id}/tracks`, `…/apple_music/{id}/tracks`, `…/soundcloud/{id}/tracks` | token | ListenBrainz's own importers, returning JSPF. |

### Reading a playlist

Each JSPF track has:
- `title`, `creator`, `album`, `duration` (ms);
- `identifier`, a **list** of URLs like
  `https://musicbrainz.org/recording/<recording MBID>`;
- an `extension` → `https://musicbrainz.org/doc/jspf#track` object with
  `added_at`, and `additional_metadata.artists[]` (`artist_credit_name`,
  `artist_mbid`, `join_phrase`), `caa_id` and `caa_release_mbid` (for cover
  art).

At playlist level, the `https://musicbrainz.org/doc/jspf#playlist` extension
has `created_for` and `additional_metadata.algorithm_metadata.source_patch`.
`source_patch` identifies the type: **`weekly-exploration`, `weekly-jams`,
`daily-jams`** (the values Explo relies on). Yearly ones like
`top-discoveries-of-2025` and `top-missed-recordings-of-2025` also appear
(Explo issue #89).

### How often they change (docs, "Data update intervals")

- Weekly playlists: "Monday morning, based on the user's timezone setting".
- Daily playlists: "Every morning, based on the user's timezone setting". To
  get daily playlists the user must **follow `troi-bot`**.
- Older generated playlists stay in the `createdfor` list for a while (one
  user had 6 listed), so always pick the newest of each type by `date`.
- Size: weekly playlists are about 50 tracks (Explo's sample config).

### Rate limits and reliability

- Docs: never more than **one call per second** per client application. Read
  the headers `X-RateLimit-Limit`, `-Remaining`, `-Reset-In` and `-Reset`.
  Exceeding the limit gives HTTP 429. "Requests with a valid user token may get
  higher limits."
- *Tested 2026-10-21* (anonymous): `X-RateLimit-Limit: 30` with a 10-second
  window.
- **2026 reliability:** the MetaBrainz blog of 2026-08-19 ("Growing pains")
  reports:
  - the cluster that generates stats and **playlists** crashed weekly from
    memory problems;
  - AI-scraper traffic surges (about 5×) made the team disable some endpoints
    and made the site unreachable at times.

  Explo issues show runs failing with timeouts and EOF errors (#89, #102,
  #138).

### Recommendation

Core feature, risk low (but plan for outages):
- poll a few hours after Monday morning in the user's timezone; don't fail
  hard;
- retry with backoff over hours, and show "waiting for ListenBrainz" in the
  UI;
- store the playlist MBID of the last import, and only rebuild when the
  newest playlist of that type has a new MBID;
- send the user token (higher limits; needed for metadata lookup);
- respect the rate-limit headers, and send a real `User-Agent`
  (`Releasomat/<version> ( <contact URL> )`).

---

## slskd (Soulseek)

**Current version:** **0.26.1** (2026-10-10); 0.26.0 came out 2026-07-19
(GitHub releases).

**Changes that matter:**
- **0.25.0** (April 2026): configuration keys moved (`global` → `transfers`,
  `integration` → `integrations`), and an old config makes slskd exit. Also
  added Docker PUID/PGID support and "Additional Terms" to its AGPL licence.
- **0.26.0** (July 2026):
  - **transfer batches** with an optional per-batch **destination folder**
    and **external ID**;
  - **automatic retries** of failed downloads
    (`transfers.download.retry.attempts`, `delay` and `max_delay`; partial
    files resumed by default);
  - a configurable **destination subdirectory**
    (`transfers.download.destination.subdirectory`, default
    `${SOURCE_DIRECTORY}`; other tokens: `SOURCE_USERNAME`, `SOURCE_PATH`,
    `BATCH_ID`, `BATCH_EXTERNAL_ID`, `SEARCH_ID`, `SEARCH_TEXT`);
  - `exists: rename|overwrite` (default **rename**: a clash appends a number to
    the new file name);
  - breaking: the file permission setting moved under `destination`.
- **0.26.1**: enqueue attempts that time out are now cancelled; logging to
  disk is on by default.

### Auth

API keys go in `web.authentication.api_keys` in `slskd.yml` (16–255
characters, optional CIDR restriction), or the primary key in
`SLSKD_API_KEY`. Send it as the **`X-API-Key`** header. Base path:
`/api/v0/`.

### Search (from `SearchesController.cs` / `SearchRequest.cs` at tag 0.26.1)

- `POST /api/v0/searches` with JSON:
  - `id` (optional GUID you choose), `searchText`;
  - `searchTimeout` (seconds since the *last* response, default 15);
  - `responseLimit` (default 100), `fileLimit` (default 10,000);
  - `filterResponses`, `maximumPeerQueueLength`, `minimumPeerUploadSpeed`,
    `minimumResponseFileCount`.
- Only **one search request is processed at a time**. A concurrent POST gets
  **429** ("Only one concurrent operation is permitted").
- `GET /api/v0/searches/{id}` returns `state`, `isComplete`, `responseCount`,
  `fileCount` and `lockedFileCount`.
- `GET /api/v0/searches/{id}/responses` returns, per user: `username`,
  `hasFreeUploadSlot`, `queueLength`, `uploadSpeed`, and `files[]` with
  `filename` (the remote path with backslashes), `size`, `bitRate`,
  `bitDepth`, `sampleRate`, `length` (s) and `extension` (unreliable: Explo
  issue #202). `lockedFiles` are listed separately.
- `DELETE /api/v0/searches/{id}` cleans up.

### Enqueue downloads (`TransfersController.cs`, 0.26.1)

- **New: `POST /api/v0/transfers/downloads/batches`**, body:
  ```json
  {
    "id": "<optional GUID>",
    "searchId": "<optional GUID>",
    "username": "<user>",
    "files": [{ "filename": "<remote path>", "size": 12345678 }],
    "options": { "destination": "<relative folder>", "externalId": "<your id>" }
  }
  ```
  It answers 201 (all queued), 207 (some), 200 (none), 409 (batch ID already
  exists) or 429 (busy). `GET /api/v0/transfers/downloads/batches/{id}` reads a
  batch back.
- **Old:** `POST /api/v0/transfers/downloads/{username}` with
  `[{filename, size}]` still works but is marked *obsolete* ("use batches").
- Both allow at most 2 concurrent enqueue requests (429 otherwise).

### Polling transfer state

- `GET /api/v0/transfers/downloads` (all users → directories → files),
  `GET /api/v0/transfers/downloads/{username}/{id}` (one transfer), or the
  batch endpoint.
- Each file has `id`, `state`, `bytesTransferred`, `bytesRemaining`,
  `percentComplete`, `averageSpeed` and timestamps.
- States look like `Requested`, `Queued, Remotely`, `Initializing`,
  `InProgress`, `Completed, Succeeded`, and the failures `Completed, Errored`,
  `Completed, Rejected`, `Completed, TimedOut`, `Completed, Cancelled`,
  `Completed, Aborted`.
- To remove a transfer from the list: `DELETE /api/v0/transfers/downloads/{username}/{id}?remove=true`.
- **Events:** slskd can call a **webhook** on `DownloadFileComplete` or
  `DownloadDirectoryComplete`. The event JSON includes **`localFilename`**,
  the actual saved path (docs/config.md).

### Where finished files land

- They go to the downloads directory (`SLSKD_DOWNLOADS_DIR`; `/app/downloads`
  in the official Docker image). Incomplete files go to the incomplete
  directory.
- The subfolder is the remote file's **immediate parent folder name** by
  default (`${SOURCE_DIRECTORY}`), or the batch `destination` if you set one.
- With `exists: rename` the final name can differ from the remote name. Read
  `localFilename` from the event rather than guessing.
- Explo guessed the location (and got empty-folder leftovers, #180, and "lost
  files" from wrong mounts, #151).

### Recommendation (risk low technically)

- Use **batches** with `options.destination = "releasomat/<job id>"` and
  `externalId = <job id>`. Each download then lands in a unique, known folder.
  Releasomat moves the file into the library afterwards.
- Run **one search at a time** (a queue), and retry 429s politely.
- On failure, take the **next candidate** (another user or file), as users
  asked of Explo in #122. slskd's own retry re-tries the *same* user.
- After the download, **check the file** (tags and duration) before
  accepting it.
- Note the setup needs: slskd's downloads folder must be mounted into the
  Releasomat container too, ideally at the same path. Soulseek is
  peer-to-peer file sharing; what users download is their own responsibility.

---

## Navidrome

**Current version:** **v0.64.2** (2026-09-24).

**Relevant history:**
- **0.58.0** (2025-07-28): multi-library;
- **0.59.0** (2025-12-06): selective folder scanning and watcher
  improvements;
- **0.61.0** (2026-03-31): FTS5 search ranking for `search3`;
- **0.62.0** (2026-06-08): the native `/api/song` path filter works with
  "starts with";
- **0.64.0** (2026-09-12): experimental Jellyfin API and security fixes.

### Auth (Subsonic API)

- Parameters on every call: `u` (user), `t` = `md5(password + salt)`, `s`
  (salt), `v` (e.g. `1.16.1`), `c` (client name, e.g. `releasomat`), `f=json`.
- This needs the **plain password** (or the legacy `p=enc:<hex>`).
- Navidrome 0.64.2 does not list an API-key auth extension. Its OpenSubsonic
  extensions include `formPost`, so **any call can be sent as a POST form**.
  Use that for long `songId` lists.

### Calls Releasomat needs

| Call | Purpose / notes |
|---|---|
| `search3?query=&songCount=&songOffset=&musicFolderId=` | Find songs. `songCount` defaults to 20. **If `query` is a UUID, Navidrome matches it exactly against the recording MBID or release-track MBID** (`persistence/sql_search.go`, `mediaFileSearchConfig`). Results include `id`, `title`, `artist`, `album`, `duration`, `musicBrainzId`, `path`, OpenSubsonic `isrc` (list) and `artists`. ISRC is returned but, as far as I can see, not searchable. |
| `createPlaylist?name=&songId=…` | Create a playlist. |
| `createPlaylist?playlistId=&songId=…` | **Replace all songs of an existing playlist and keep its ID** (Navidrome `core/playlists/playlists.go`, `Create`: it clears the tracks and adds the given IDs). Only the owner or an admin may do this. This is the way to "update in place" so syncing apps don't see a new playlist. |
| `updatePlaylist?playlistId=&name=&comment=&public=&songIdToAdd=&songIndexToRemove=` | Rename, describe, add or remove single entries. |
| `getPlaylists`, `getPlaylist?id=`, `deletePlaylist?id=` | Housekeeping. Store the playlist ID; don't look it up by name. |
| `startScan?fullScan=false&target=<libraryId>:<folder>` | **Admin only.** `target` (repeatable) scans just that folder, relative to the library root (selective scan, 0.59+). The call waits up to 3 s for the scan to start, so the next status call is accurate. |
| `getScanStatus` | `scanning`, `count`, `folderCount`, `lastScan`, `scanType`, `elapsedTime`, `error`. Poll until `scanning=false`. |
| `getMusicFolders` | Lists the libraries (one per library in multi-library mode). |

### The `path` field: can Releasomat find its files by path?

*From the Navidrome source, v0.64.2 (`server/subsonic/helpers.go`):*
- **By default the Subsonic `path` is fake**: built from tags as
  `AlbumArtist/Album/DD-TT - Title.ext`. It does *not* match the file on
  disk. Explo issue #163 shows exactly this.
- If **"Report Real Path"** is enabled for the player (the Players admin
  page; `ND_SUBSONIC_DEFAULTREPORTREALPATH=true` turns it on for *new*
  players), the path is **absolute on Navidrome's filesystem** (library path +
  relative path, e.g. `/music/Releasomat/…`). It is **not** relative to the
  library root. Releasomat would need a path mapping between Navidrome's mount
  and its own. It's off by default for security reasons (docs).
- **The native API** (the one Navidrome's web UI uses; undocumented and not
  promised to be stable) is better for this:
  - log in with `POST /auth/login` (`{username, password}` → `token`);
  - then send `X-ND-Authorization: Bearer <token>`;
  - `GET /api/song?path=<prefix>&library_id=<id>` filters by path "starts
    with". In the database, **`path` is relative to the library root** and
    the library's own path is a separate field (`model/mediafile.go`,
    `AbsolutePath()` joins the two).

**Recommended way to find a song Releasomat just downloaded:**
1. Write the **recording MBID** into the file (when known), then use
   `search3?query=<MBID>`. Exact, uses the stable Subsonic API, and Navidrome's
   track ID becomes stable too (`PID.Track` defaults to `musicbrainz_trackid`
   first).
2. Otherwise use the native API with `path` + `library_id`, or the Subsonic
   API with Report Real Path on plus a path mapping.
3. Fall back to fuzzy `search3` by title + artist (plus a duration check).

### How fast new files appear

- **Watcher:** `Scanner.WatcherWait` (default **5 s**) starts a scan of the
  changed folder after a file change. That only works if file-system events
  reach Navidrome's container. *Unverified:* reliability on Unraid's
  `/mnt/user` (FUSE) mounts across two containers.
- So Releasomat should still call **`startScan` with `target=`** for the
  folder it wrote, then poll `getScanStatus`. A one-folder targeted scan
  should take seconds. I found no official timing numbers. A full scan of a
  large library can take minutes.
- After a scan, songs are searchable right away (same database).
- Deleted files become "missing". They are not purged by default
  (`Scanner.PurgeMissing = "never"`).

### Multi-library (0.58+)

- Admins add libraries in **Settings → Libraries**. Each is a path Navidrome
  can read. **Libraries must not overlap.**
- `getMusicFolders` shows the libraries the user can access. Other Subsonic
  endpoints respect per-user library permissions. Playlists may mix songs
  from several libraries. Admins see all libraries.
- For Releasomat: either put downloads *inside* the main library (e.g.
  `/music/Releasomat/…`), or give them their own library whose path is
  *outside* the main one, never nested.

### Other Navidrome options worth knowing

`.m3u` and smart-playlist (`.nsp`) auto-import (`AutoImportPlaylists`,
`PlaylistsPath`) is an alternative to the API. But file-based "synced"
playlists can't have tracks edited through the API (0.64.0 fixed a case where
that was allowed). Prefer the API.

**Recommendation:** Subsonic API for playlists and scans (needs an admin user,
or the user's own account plus an admin account just for `startScan`). Native
API optional, for path lookups. Risk low.

---

## yt-dlp and ytmusicapi as Python libraries

### yt-dlp (latest 2026.08.19; Unlicense, so fine to use from AGPL code)

**Use it in-process** as a library:
- create `yt_dlp.YoutubeDL(options)`, then call `.extract_info(url, download=True|False)`;
- **`progress_hooks`**: a list of functions that get a dict with `status`
  (`downloading` / `finished` / `error`), `downloaded_bytes`, `total_bytes`,
  `filename` and so on. Use it for progress bars;
- **`postprocessor_hooks`**: the same, for post-processing steps;
- other useful options:
  - `format: "bestaudio/best"`, `outtmpl` / `paths`, `noplaylist`;
  - `extract_flat: "in_playlist"`, for listing playlists;
  - `postprocessors`: for example `FFmpegExtractAudio` with
    `preferredcodec: "best"` (keeps Opus/M4A without re-encoding), plus
    `FFmpegMetadata` and `EmbedThumbnail`;
  - `cookiefile`, `sleep_interval` / `sleep_requests`, `ratelimit`;
  - `extractor_args: {"youtube": {"player_client": [...]}}`.

**YouTube requirements in 2026:**
- **JavaScript runtime + yt-dlp-ejs.** Both are needed for full YouTube
  support. Deno is recommended and is the only runtime enabled by default.
  Since 2026.06.09 the minimum is **Deno ≥ 2.3.0**; Node ≥ 22 works if
  enabled; Bun is deprecated. `pip install "yt-dlp[default]"` pulls in
  `yt-dlp-ejs`. Without a runtime, YouTube "still works to some extent" with
  fewer formats.
- **Default player clients** (README, 2026): `tv_samsung,visionos,web`. Without
  a JS runtime, `web` and `tv_samsung` are dropped.
- **PO tokens** (wiki, updated 2026-07-12): needed for media (GVS) requests by
  `web`, `web_safari`, `mweb`, `web_music`, `android` and `ios`. Not needed by
  `tv`, `web_embedded` or `android_vr`. The wiki's "TL;DR recommended setup"
  is a **PO token provider plugin** with the `mweb` client. Manual tokens are
  no longer recommended (they are bound to each video ID). The main provider
  is **bgutil-ytdlp-pot-provider**, now 2.0.2 (2026-10-07). Version 2.0.0
  fixed a **remote-code-execution hole** in its HTTP server and now binds to
  localhost only. Keep it updated and never expose it.
- **Cookies** are optional. The wiki warns: "By using your account with
  yt-dlp, you run the risk of it being banned (temporarily or permanently)."
  It suggests **5–10 s sleep between downloads**. Guest sessions are limited
  to about **300 videos/hour**, accounts to about 2,000. If cookies are used,
  export them from a private window, not with `--cookies-from-browser`.
- Python: 3.10 support is ending (EOL October 2026); **3.11+ recommended**.

**Keeping yt-dlp current inside the container:**
- Releases come every few weeks, and YouTube changes break old versions within
  weeks (the Explo issues show this).
- `yt-dlp -U` does not apply to pip installs; upgrade with
  `pip install -U "yt-dlp[default]"` (plus the bgutil plugin).
- An upgrade **doesn't affect a Python process that has already imported
  yt-dlp**. So run downloads in a **worker subprocess** (e.g. a
  `multiprocessing` worker) that is restarted after an upgrade, or restart
  the app after a scheduled update.
- Show the yt-dlp version in the UI, and pin the Deno version in the
  Dockerfile.

### ytmusicapi (1.12.3, MIT)

- `YTMusic()` without auth is enough for search and public playlists.
- **Search with `filter="songs"`**: results carry `videoId`, `title`,
  `artists`, `album`, `duration_seconds`, `isExplicit`, and (since 1.12.3)
  `isAvailable`. Releasomat can check artist and duration instead of taking
  the first hit as Explo does.
- `get_playlist(id, limit=None)` gets all tracks of a playlist.
- Search on YouTube Music, but **download through yt-dlp**. YouTube Music
  formats (`web_music`) need PO tokens, so a plain `youtube.com/watch?v=<id>`
  URL with default clients is usually simpler.

---

## Matching

### Libraries

- **rapidfuzz** 3.14.6 (2026-08-30, MIT): fast fuzzy string matching.
  `fuzz.token_set_ratio` / `token_sort_ratio` handle word order and extra
  words; `fuzz.WRatio` is a good general score; `utils.default_process`
  lowercases and strips punctuation.
- Accent folding: the standard library (`unicodedata.normalize("NFKD", …)`,
  then drop combining marks) needs no dependency. `Unidecode` is GPL, which is
  compatible with AGPL but not needed.
- Tag reading and writing: **mutagen** 1.48.1 (GPL-2.0-or-later, compatible
  with AGPL-3.0-or-later). It reads duration and tags of downloaded files for
  the "is this the right song?" check, and writes MusicBrainz IDs and ISRC.

### ISRC availability by source (as of 2026-10-21)

| Source | ISRC? | Where |
|---|---|---|
| ListenBrainz | no ISRC in JSPF, but **recording MBID** (better) | `identifier` |
| Deezer | yes, every track (tested) | `isrc` |
| Tidal | yes (spec) | track `isrc` |
| Spotify official API | yes (reverted in March 2026) | `external_ids.isrc` |
| Spotify embed / pathfinder | no | — |
| Apple Music page JSON | no | — |
| Apple amp-api / official API | yes (tested) | `attributes.isrc` |
| Beatport page JSON | yes (tested) | `isrc` |
| YouTube / YT Music | no | — |
| SoundCloud | rarely | — |
| Navidrome | if the file is tagged | OpenSubsonic `isrc` list on songs |

### MusicBrainz lookups

- **By ISRC:** `GET https://musicbrainz.org/ws/2/isrc/{ISRC}?fmt=json&inc=artist-credits+releases`
  returns the recordings with that ISRC. One ISRC can map to several
  recordings, and vice versa.
- **Rate limit** (official, checked 2026-10-21): about **1 request per second
  per IP** on average. Above that, *all* requests get **HTTP 503** until the
  rate drops. A **meaningful User-Agent** with contact info is required
  (format `AppName/1.2.0 ( contact )`).
- **By name:** ListenBrainz `POST /1/metadata/lookup/` (token) maps artist +
  title (+ release) to a recording MBID in one batch request. Its own docs
  note that a 200 response doesn't guarantee a match, so check the returned
  names.

### Suggested matching order for Releasomat

1. **Recording MBID** (ListenBrainz tracks, or derived via ISRC or the lookup).
   Then `search3?query=<MBID>` in Navidrome.
2. **ISRC**: search Navidrome by title, then compare each candidate's `isrc`.
3. **Fuzzy**: normalised title (strip feat./remaster/"- Radio Edit" only when
   the source doesn't specify a version), main artist, album (bonus only),
   duration within about 3–10 s. Keep **mix names** for DJ sources (Beatport):
   "Extended Mix" ≠ "Radio Edit".
4. After downloading, **verify the file** against the wanted track (tags and
   duration). Reject and try the next candidate on a mismatch.

---

## Sources

Checked 2026-10-21 unless a date is given.

**Spotify**
- Changes to the Web API (2024-11-27): https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api
- Updating the criteria for Web API extended access (2025-04-15): https://developer.spotify.com/blog/2025-04-15-updating-the-criteria-for-web-api-extended-access
- Quota modes: https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- Update on Developer Access and Platform Security (2026-02-06): https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security
- February 2026 migration guide: https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide
- Web API changelog March 2026: https://developer.spotify.com/documentation/web-api/references/changes/march-2026
- Web API changelog May 2026: https://developer.spotify.com/documentation/web-api/references/changes/may-2026
- Web API changelog July 2026: https://developer.spotify.com/documentation/web-api/references/changes/july-2026
- Web API quota updates for Development Mode (2026-07-23): https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates
- Get Playlist Items reference: https://developer.spotify.com/documentation/web-api/reference/get-playlists-items
- TechCrunch on the Feb 2026 change: https://techcrunch.com/2026/02/06/spotify-changes-developer-mode-api-to-require-premium-accounts-limits-test-users/
- SpotifyScraper (keyless approach): https://github.com/AliAkhtari78/SpotifyScraper
- Embed page tested: https://open.spotify.com/embed/playlist/37i9dQZF1DX4UtSsGT1Sbe

**Apple Music**
- Apple developer forum, free access to Apple Music API: https://developer.apple.com/forums/thread/120537
- Apple developer forum, third-party playlist via API: https://developer.apple.com/forums/thread/123894
- Algoriddim forum, Apple Music playlists load 100 at a time: https://community.algoriddim.com/t/apple-music-playlist-with-limit-100-songs/34034
- Hack Club devlog about the index.js move after WWDC 2026: https://stardance.hackclub.com/projects/1186
- Reverse-engineering notes: https://minitor.is-a.dev/blog/apple-music-api
- Pages tested: https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb and https://music.apple.com/gb/playlist/10-years-of-apple-music-top-songs/pl.c125e4b2998a4015abbe6b9ae90d5479

**Deezer, Tidal, SoundCloud, Qobuz, Beatport, Odesli**
- Deezer API endpoint tested: https://api.deezer.com/playlist/3155776842
- Rocksky Deezer service (rate-limit note): https://next.tangled.org/rocksky.app/rocksky/tree/main/deezer
- TIDAL OpenAPI spec: https://github.com/tidal-music/tidal-sdk-web/blob/main/packages/api/bin/tidal-api-oas.json
- TIDAL developer portal: https://developer.tidal.com
- SoundCloud self-serve API keys: https://developers.soundcloud.com/blog/vibe-coding-ai-agent-docs-self-serve-api-keys
- SoundCloud register an app: https://developers.soundcloud.com/docs/api/register-app
- SoundCloud rate limits: https://developers.soundcloud.com/docs/api/rate-limits
- Qobuz API (directory): https://providers.apievangelist.com/providers/qobuz/
- minim (private Qobuz/TIDAL API notes): https://minim.readthedocs.io/en/latest/api/minim.qobuz.PrivateAPI.html
- Beatport API (directory): https://providers.apievangelist.com/providers/beatport/
- beets-beatport4: https://github.com/Samik081/beets-beatport4
- Beatport Top 100 page tested: https://www.beatport.com/top-100
- Odesli API notice: https://linktree.notion.site/API-d0ebe08a5e304a55928405eb682f6741

**ListenBrainz / MusicBrainz**
- Playlist API: https://listenbrainz.readthedocs.io/en/latest/users/api/playlist.html
- API index and rate limiting: https://listenbrainz.readthedocs.io/en/latest/users/api/index.html
- Metadata API: https://listenbrainz.readthedocs.io/en/latest/users/api/metadata.html
- Data update intervals: https://listenbrainz.readthedocs.io/en/latest/general/data-update-intervals.html
- MetaBrainz blog, "Growing pains" (2026-08-19): https://blog.metabrainz.org/2026/08/19/growing-pains-an-update-on-the-listenbrainz-service-status/
- MusicBrainz rate limiting: https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
- JSPF extensions: https://musicbrainz.org/doc/jspf

**slskd**
- Releases (0.25.0, 0.26.0, 0.26.1): https://github.com/slskd/slskd/releases
- TransfersController.cs (0.26.1): https://github.com/slskd/slskd/blob/0.26.1/src/slskd/Transfers/API/Controllers/TransfersController.cs
- SearchesController.cs (0.26.1): https://github.com/slskd/slskd/blob/0.26.1/src/slskd/Search/API/Controllers/SearchesController.cs
- EnqueueDownloadBatchRequest.cs: https://github.com/slskd/slskd/blob/0.26.1/src/slskd/Transfers/API/DTO/EnqueueDownloadBatchRequest.cs
- Configuration docs: https://github.com/slskd/slskd/blob/0.26.1/docs/config.md

**Navidrome**
- Releases: https://github.com/navidrome/navidrome/releases
- Configuration options: https://www.navidrome.org/docs/usage/configuration/options/
- Multi-library: https://www.navidrome.org/docs/usage/features/multi-library/
- Source v0.64.2, `server/subsonic/helpers.go` (path reporting): https://github.com/navidrome/navidrome/blob/v0.64.2/server/subsonic/helpers.go
- Source, `server/subsonic/library_scanning.go` (startScan/target): https://github.com/navidrome/navidrome/blob/v0.64.2/server/subsonic/library_scanning.go
- Source, `core/playlists/playlists.go` (createPlaylist with playlistId): https://github.com/navidrome/navidrome/blob/v0.64.2/core/playlists/playlists.go
- Source, `persistence/sql_search.go` and `mediafile_repository.go` (MBID search, path filter): https://github.com/navidrome/navidrome/blob/v0.64.2/persistence/sql_search.go
- Source, `server/subsonic/opensubsonic.go` (extensions): https://github.com/navidrome/navidrome/blob/v0.64.2/server/subsonic/opensubsonic.go

**yt-dlp / ytmusicapi / libraries**
- yt-dlp README (embedding, extractor args): https://github.com/yt-dlp/yt-dlp/blob/master/README.md
- yt-dlp releases (2026.06.09, 2026.07.04, 2026.08.19): https://github.com/yt-dlp/yt-dlp/releases
- PO Token Guide (updated 2026-07-12): https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
- Extractors wiki (YouTube cookies, rate limits): https://github.com/yt-dlp/yt-dlp/wiki/Extractors
- EJS wiki: https://github.com/yt-dlp/yt-dlp/wiki/EJS
- bgutil-ytdlp-pot-provider releases: https://github.com/Brainicism/bgutil-ytdlp-pot-provider/releases
- ytmusicapi releases: https://github.com/sigma67/ytmusicapi/releases
- ytmusicapi setup docs: https://ytmusicapi.readthedocs.io/en/stable/setup/index.html
- ytmusicapi playlists reference: https://ytmusicapi.readthedocs.io/en/stable/reference/playlists.html
- PyPI: https://pypi.org/project/rapidfuzz/ · https://pypi.org/project/yt-dlp/ · https://pypi.org/project/ytmusicapi/ · https://pypi.org/project/mutagen/

**Explo (for cross-references)**
- https://github.com/LumePart/Explo (v1.2.0) and its issues #79, #89, #92, #102, #122, #138, #141, #151, #163, #180, #202
