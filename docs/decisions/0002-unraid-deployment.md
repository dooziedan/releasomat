# 0002: One container on Unraid
Date: 2026-10-21 · Status: Accepted

## Context
The owner runs Navidrome (and Tagwerk) on Unraid. Releasomat must install like any Unraid app: one
image, a template, folders mapped by the user.

## Decision
- One image `ghcr.io/dooziedan/releasomat`, built by CI from git tags. Port **8430**.
- Container paths:
  - `/config`: database, yt-dlp `cookies.txt`, cache. Everything Releasomat needs to survive an update.
  - `/music`: Releasomat's **own folder inside the Navidrome library** (for example
    `/mnt/user/media/music/Releasomat`). Releasomat never sees the rest of the library on disk; it
    finds existing songs through Navidrome's API.
  - `/slskd` (optional): slskd's download folder, so finished Soulseek downloads can be moved into
    `/music`.
- `PUID`/`PGID`/`UMASK` (defaults `99`/`100`/`022`), handled by `docker/entrypoint.sh`. The container
  never `chown`s mapped folders in bulk.
- Container settings come from env vars. User settings (connections, defaults) live in the database
  and can be edited in the UI; an env var with the same name wins (so the Unraid template can set them).
- Other services are reached by host IP or Docker network name, never `localhost`.
- `TZ` is not in the Unraid template: Unraid passes its own time zone to every container. The image
  defaults to `UTC` elsewhere.
- Everything Releasomat needs at runtime is inside the image: Python, ffmpeg, yt-dlp, ytmusicapi.

## Consequences
- Mapping `/music` to a subfolder limits what a bug could touch (see ADR 0003).
- Navidrome must scan that subfolder, so it has to be inside Navidrome's music folder, and Releasomat
  needs to know its name there (`NAVIDROME_SUBFOLDER`).
