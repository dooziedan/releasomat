# 0010: Adopted songs stay the same song in Navidrome
Date: 2026-10-30 · Status: Accepted (owner's decision) for Releasomat; Tagwerk's part proposed

## Context
A download that the owner adopts in Tagwerk (ADR 0009) is moved into Tagwerk's import inbox,
improved there (title, album, BPM, key, genre, IDs) and imported into the library. The owner wants
such a track to **stay in its playlists**, and **Navidrome must not turn it into a second song**.
Until now Navidrome usually saw a new song: the old one stayed behind as "missing" (with the plays,
stars and rating), the track left the Releasomat playlist while it waited in the inbox, and the
next refresh linked the new song.

**How Navidrome recognises a moved file** (0.55 and later; tried on 0.64.2): when a file
disappears, its song is marked *missing*. It is kept, not deleted (with `Scanner.PurgeMissing` =
`never`, the default), and stays in every playlist, hidden. When a new file turns up with the same
**persistent ID** (PID), it takes over the missing song: same ID, same playlists, plays, stars and
rating. The defaults are:
- `PID.Track` = `musicbrainz_trackid|albumid,discnumber,tracknumber,title`
- `PID.Album` = `musicbrainz_albumid|albumartistid,album,albumversion,releasedate`

Navidrome's `musicbrainz_trackid` is the MusicBrainz **release track** ID
(`MUSICBRAINZ_RELEASETRACKID`). The file tag `MUSICBRAINZ_TRACKID` that Releasomat writes is the
**recording** ID (Navidrome calls it `musicbrainz_recordingid`), and it isn't part of the PID. So a
Releasomat download is recognised by **album artist, album, album version, release date, disc and
track number, and title**.

**Tried against the dev Navidrome 0.64.2**: a download in Releasomat's folder, in a playlist,
starred, rated and played once; moved to an "inbox" outside the library (two scans), changed
there, moved into the library, scanned:

| Changed in the inbox | Same song (ID, playlist, star, rating, plays)? |
|---|---|
| genre, BPM, key | yes |
| a MusicBrainz recording ID added | yes |
| FLAC converted to AIFF, same tags | yes |
| title and album (with or without a MusicBrainz ID) | **no**: a new song, the old one stays missing |
| title, album and MusicBrainz ID, **in two steps**: moved unchanged, scanned, then the tags changed in place and scanned again | yes |

Also seen: while the file is away, Navidrome hides the song in the playlist but keeps its place. It
shows it again there as soon as the file comes back with the same ID. A playlist can be saved with
the IDs of missing songs (they stay hidden); IDs Navidrome doesn't know are dropped silently.

## Decision
**Releasomat** (built):
- An adopted track **keeps its old song in the Navidrome playlist** while its file waits in
  Tagwerk's inbox (`TrackState.held_id`). Navidrome hides it meanwhile; the run says "2 songs + 1
  adopted song waiting for your library".
- Each refresh first asks Navidrome about that old song (`Navidrome.song_elsewhere`, Navidrome's
  own API). If it is in the library outside Releasomat's folder, it is linked as it is
  (`matched_by: same_song`; the playlist page says "Navidrome kept the song, with its plays and
  stars"). Otherwise the search by MusicBrainz ID, ISRC, name and length finds a library copy as
  before (ADR 0009), and that song replaces the old one in its place. If Navidrome's own API
  doesn't answer, the search still works.
- A song the owner threw away in Tagwerk's inbox ("Don't like", `discarded`) doesn't keep its
  place.
- Nothing new is written into files (ADR 0009 stays: no origin note), and the integration API
  doesn't change.

**Tagwerk** (proposed; Tagwerk's own decision record and code): **import an adopted file in two
steps**, like Tagwerk's "Navidrome first" rule for renaming, converting and better copies (Tagwerk
ADR 0012, 0013, 0037):
1. move the file, with the tags it had in Releasomat's folder, to its place in the library;
2. let Navidrome scan and wait until it lists the song at the new place;
3. write the new tags into the file there and let Navidrome scan again.

If Navidrome doesn't confirm in time, Tagwerk writes the tags anyway: Navidrome then makes a new
song, and Releasomat links it on its next refresh (as before this decision).

**Navidrome**: `Scanner.PurgeMissing` (`ND_SCANNER_PURGEMISSING`) stays `never`, the default. With
`always` (or `full`) Navidrome deletes the missing song while its file waits in Tagwerk's inbox, and
the imported file becomes a new song.

## Not chosen
- **A Releasomat ID in the file** that Navidrome could use as the PID: the owner decided against any
  origin note in files (ADR 0009).
- **Writing the MusicBrainz release track ID**: Releasomat rarely knows which release a track
  belongs to, and Tagwerk may fill in or correct that ID on import, which changes the PID as well.
- **Changing Navidrome's PID setting**: it applies to the whole library on the owner's server and
  doesn't help when the import changes the tags it is built from.
- **Removing Navidrome's missing songs through its own API**: plays and stars would still be lost,
  and Navidrome's "Missing files" page is the owner's to tidy.

## Consequences
- With Tagwerk's two-step import an adopted song stays the same song everywhere: Releasomat's
  playlists, its old snapshots and the owner's own playlists, with plays, stars and rating, and no
  second song in Navidrome.
- Until Tagwerk imports that way, or when an import changes title or album in one go, Navidrome
  makes a new song. Releasomat links it on its next refresh, in the same place, and the old song
  stays on Navidrome's "Missing files" page until the owner removes it there.
- While the file waits in the inbox it can't be played: Navidrome hides it in the playlist.
- One more call to Navidrome per adopted track and refresh.
