#!/bin/sh
# Start Releasomat as the user/group given by PUID/PGID (Unraid default: 99/100 = nobody:users),
# so the files it creates in /music and /config get the owner Unraid expects (ADR 0002).
set -e

umask "${UMASK:-022}"

if [ "$(id -u)" != "0" ]; then
    # Started with an explicit --user: nothing to switch.
    exec "$@"
fi

PUID="${PUID:-99}"
PGID="${PGID:-100}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

# Only the app data folder itself is handed to that user (not recursively, nothing inside it).
# The music and slskd folders are never chowned: they belong to the owner (CLAUDE.md).
mkdir -p "$CONFIG_DIR"
chown "$PUID:$PGID" "$CONFIG_DIR"

echo "Starting Releasomat as UID=$PUID GID=$PGID UMASK=$(umask)"
export HOME="$CONFIG_DIR"
# Deno (yt-dlp's JavaScript runtime) keeps its cache in the app data folder, like yt-dlp.
export DENO_DIR="${DENO_DIR:-$CONFIG_DIR/cache/deno}"
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
