# Releasomat

Turn playlists from Apple Music, ListenBrainz, Deezer, Tidal, YouTube, SoundCloud and Beatport into
playlists in your self-hosted [Navidrome](https://www.navidrome.org/). Releasomat links the songs you
already have, downloads the missing ones from Soulseek (through your own
[slskd](https://github.com/slskd/slskd)) or YouTube (via [yt-dlp](https://github.com/yt-dlp/yt-dlp)),
and keeps every playlist up to date on its own schedule.

Inspired by [Explo](https://github.com/LumePart/Explo). Runs as one Docker container, made for Unraid.

> **Status:** early development and testing. Things change often; see the [roadmap](docs/roadmap.md)
> and the [changelog](CHANGELOG.md).

## What it does

- **Playlists from links**: paste a playlist link from Apple Music, Deezer, Tidal, YouTube Music,
  YouTube, SoundCloud, Beatport (charts, Top 100) or ListenBrainz, or follow your ListenBrainz
  "Created for you" playlists (Weekly Exploration, Weekly Jams, Daily Jams). Spotify isn't supported.
- **Your library first**: songs already in Navidrome are linked, never downloaded again.
- **Download sources in your order** (Settings → Download sources): for example Soulseek first, with
  your wanted formats (FLAC, AIFF, WAV, MP3 320 …), then YouTube. Every download is checked to be the
  right song and length before it is tagged and saved.
- **Navidrome playlists**: one playlist kept up to date (same playlist, so apps that sync playlists
  don't see a new one each time), or a dated snapshot per refresh.
- **Schedules per playlist**, **Refresh now** and **Stop**, live progress, and an error report with a
  **Copy report** button whenever a service, a download or Navidrome fails.
- **Never deletes music on its own.** Releasomat writes only into its own folder inside your library.

## Install on Unraid

1. Open the Unraid **Terminal** (top right `>_` icon) and download the template once:
   ```sh
   wget -O /boot/config/plugins/dockerMan/templates-user/my-releasomat.xml \
     https://raw.githubusercontent.com/dooziedan/releasomat/main/unraid/releasomat.xml
   ```
2. Go to **Docker → Add Container**, choose **Releasomat** in the *Template* dropdown (under user
   templates) and fill in:

   | Field | What to enter |
   |---|---|
   | **Releasomat Music Folder** (required) | A folder **inside** the music folder your Navidrome container uses, for example `/mnt/user/music/Releasomat`. Releasomat only ever writes here. |
   | App Data | Leave the default `/mnt/user/appdata/releasomat` (database, settings, YouTube cookies) |
   | slskd Downloads (for Soulseek) | The folder where your slskd container saves finished downloads, as Unraid sees it, for example `/mnt/user/downloads/slskd`. |
   | WebUI Port | `8430`, or any free port |
   | PUID / PGID (advanced) | Unraid's defaults `99` / `100`. **With Soulseek: the same user and group your slskd container runs as**, so Releasomat can pick up its downloads. |
   | Everything else (advanced) | Optional: all settings can also be made in the web UI. A value set here locks the field there. |

3. Click **Apply** and open the WebUI.

The image is `ghcr.io/dooziedan/releasomat:edge`: the newest state of the `main` branch. Version
tags (`:latest`, `:X.Y.Z`) follow with the first release.

## First steps

1. **Settings → Navidrome**: its address as the container sees it (for example
   `http://192.168.1.10:4533`, never `localhost`), a Navidrome user with **admin** rights (needed to
   start a scan) and the name of Releasomat's folder inside the library (`Releasomat` in the example
   above). Press **Test connection**.
2. **Settings → Download sources**: switch sources on or off and put them in order. For Soulseek, add
   slskd's address and API key, your wanted formats and minimum bitrate, then **Test**.
3. **Add playlist**: paste a link (the service is recognised), or follow a ListenBrainz "Created for
   you" series with your ListenBrainz user name.
4. On the playlist's page press **Refresh now** and watch the tracks come in. Set a schedule there to
   keep it up to date. **Stop** cancels a running refresh; what was downloaded stays.

**YouTube** sometimes asks to "confirm you're not a bot". Then put a `cookies.txt` (Netscape format,
exported from a browser logged in to a spare account) into the App Data folder. Without an account,
YouTube allows roughly 300 downloads per hour.

## Documentation

- [Architecture](docs/architecture.md) · [Decisions](docs/decisions/) · [Roadmap](docs/roadmap.md)
- [Development](docs/development.md): local setup with a test Navidrome and a fake slskd

## License

[GNU AGPL-3.0-or-later](LICENSE). See [NOTICE](NOTICE).
