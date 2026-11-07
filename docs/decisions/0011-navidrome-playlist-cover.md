# 0011: The source's cover for the Navidrome playlist
Date: 2026-11-07 · Status: Accepted (owner's request)

## Context
Playlists on Apple Music, Deezer, Tidal, YouTube, SoundCloud and Beatport have a cover of their
own. Releasomat already reads it (`PlaylistSnapshot.cover_url`, shown on its own pages), but the
Navidrome playlist only had Navidrome's own picture: tiles made from the songs' album covers. The
owner wants the original cover in Navidrome too.

**What Navidrome offers** (tried against the dev Navidrome 0.64.2): its own API (not Subsonic)
takes a picture for a playlist, `POST /api/playlist/<id>/image` with the form field `image`, and
`DELETE` removes it again. Only the playlist's owner may, and only with `ND_ENABLEARTWORKUPLOAD` on
(its default) or as an admin; a normal user could upload with the defaults. Navidrome names the
stored picture after the playlist (`<id>_<name>.<ext>`), and a new upload replaces it under the
same name, so Releasomat **can't tell** whether the owner put a picture of their own in its place.

## Decision
- After every refresh that saved the Navidrome playlist, Releasomat gives it the source's cover
  (`pipeline.Refresh.set_cover`, `Navidrome.set_playlist_image`), in update mode and for every new
  snapshot.
- **The same picture is sent only once per Navidrome playlist.** Releasomat remembers what it sent
  (`playlist.navidrome_cover_sent` = `<navidrome id>:<sha256 of the picture>`). A picture the
  owner chooses in Navidrome stays until the source changes its cover (or the Navidrome playlist
  is made anew).
- **A switch per playlist**, on by default (`playlist.navidrome_cover`, "Use the playlist's cover
  from …" on the playlist page, `navidrome_cover` in `PATCH /api/playlists/<id>`). Off: Releasomat
  never touches Navidrome's picture.
- **ListenBrainz has no cover of its own**: Releasomat shows the first song's album cover for it,
  but doesn't give that to Navidrome (`PlaylistSnapshot.own_cover = False`); Navidrome's tiles are
  better. The switch isn't shown for ListenBrainz playlists.
- Only JPEG and PNG up to 5 MB (the same check as for covers in files).
- **A cover never fails a refresh.** When the picture can't be loaded, or Navidrome refuses it (an
  older Navidrome without the call, `ND_ENABLEARTWORKUPLOAD` off for a non-admin user), the run's
  log says so in plain words and the next refresh tries again. It isn't an error report: the
  playlist itself is fine.

## Consequences
- One more download per refresh (the cover), small next to the songs.
- An owner who wants their own picture in Navidrome for good switches the cover off for that
  playlist.
- Releasomat never removes a picture from Navidrome (no `DELETE`): switching off leaves the last
  one there.
