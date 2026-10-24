#!/usr/bin/env bash
# Prepares everything for local development and starts the dev Navidrome:
#   1. creates the dev/ folders (git-ignored)
#   2. creates dev/.env with new random test passwords, if it doesn't exist yet
#   3. creates the demo library (dev/library/Demo)
#   4. starts the dev Navidrome (docker-compose.dev.yml) and waits until it answers
#   5. makes sure its admin user from dev/.env exists
#   6. lets Navidrome scan the library
# Safe to run again at any time: it only adds what is missing. Passwords are never printed.
# Details: docs/development.md
set -euo pipefail

cd "$(dirname "$0")/.."  # the Releasomat folder, wherever the script is started from

COMPOSE=(docker compose -f docker-compose.dev.yml)
NAVIDROME=http://localhost:4533
ENV_FILE=dev/.env

say() { printf '==> %s\n' "$*"; }
fail() {
	printf 'Error: %s\n' "$*" >&2
	exit 1
}

for tool in docker python3 ffmpeg curl md5sum; do
	command -v "$tool" >/dev/null || fail "$tool is not installed."
done

# --- 1. Folders -----------------------------------------------------------------------------------
# Created here, as you, so Docker doesn't create them as root.
mkdir -p dev/library/Demo dev/library/Releasomat dev/config dev/config-docker \
	dev/slskd dev/navidrome-data dev/screenshots

# --- 2. dev/.env ----------------------------------------------------------------------------------
random_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

# env_value NAME FILE: the value of NAME in FILE. Also finds a commented-out "# NAME=value" line,
# so the script keeps working when you comment the Navidrome login out to test the setup wizard.
env_value() {
	local value
	value=$(sed -n -E "s/^[[:space:]]*$1=//p" "$2" | tail -n 1)
	if [[ -z $value ]]; then
		value=$(sed -n -E "s/^[[:space:]]*#[[:space:]]*$1=//p" "$2" | tail -n 1)
	fi
	value=${value%$'\r'}
	value=${value#\"} && value=${value%\"}
	value=${value#\'} && value=${value%\'}
	printf '%s' "$value"
}

if [[ ! -f $ENV_FILE ]]; then
	say "Creating $ENV_FILE with new random test passwords"
	(
		umask 077 # only you can read it
		cat >"$ENV_FILE" <<EOF
# Settings for running Releasomat locally (uvicorn --env-file dev/.env). Dev-only test values.
# Made by scripts/dev_up.sh; never commit this file. A setting that is set here is locked
# (read-only) on the Settings page; comment it out to edit it there or to test the setup wizard.

# Folders (relative to the Releasomat folder)
CONFIG_DIR=dev/config
MUSIC_DIR=dev/library/Releasomat
SLSKD_DOWNLOADS_DIR=dev/slskd
TZ=Europe/Berlin
LOG_LEVEL=DEBUG

# The dev Navidrome from docker-compose.dev.yml (this is also its admin login)
NAVIDROME_URL=http://localhost:4533
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=$(random_secret)
NAVIDROME_SUBFOLDER=Releasomat

# Your ListenBrainz user name, for your own playlists and "Created for you"
# LISTENBRAINZ_USER=
# ListenBrainz user token (listenbrainz.org/settings), only needed for private playlists
# LISTENBRAINZ_TOKEN=

# Tidal app (developer.tidal.com), only needed to read Tidal playlists
# TIDAL_CLIENT_ID=
# Secret of that Tidal app
# TIDAL_CLIENT_SECRET=
EOF
	)
fi

ND_USER=$(env_value NAVIDROME_USER "$ENV_FILE")
ND_PASSWORD=$(env_value NAVIDROME_PASSWORD "$ENV_FILE")
[[ -n $ND_USER && -n $ND_PASSWORD ]] ||
	fail "NAVIDROME_USER or NAVIDROME_PASSWORD is missing in $ENV_FILE."

# --- 3. Demo library ------------------------------------------------------------------------------
# Before Navidrome starts: a new Navidrome scans once by itself right away.
python3 scripts/make_demo_library.py

# --- 4. Start Navidrome ---------------------------------------------------------------------------
say "Starting the dev Navidrome"
"${COMPOSE[@]}" up -d navidrome

for _ in $(seq 60); do
	curl -fsS -o /dev/null "$NAVIDROME/ping" 2>/dev/null && break
	sleep 1
done
curl -fsS -o /dev/null "$NAVIDROME/ping" ||
	fail "Navidrome did not start. See: docker compose -f docker-compose.dev.yml logs navidrome"

# subsonic ENDPOINT [PARAM=VALUE ...]: calls Navidrome's Subsonic API as the dev admin and prints
# the JSON answer. Logs in with a token (md5 of password + random salt), never the password itself.
subsonic() {
	local endpoint=$1 salt token param
	shift
	salt=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
	token=$(printf '%s%s' "$ND_PASSWORD" "$salt" | md5sum | cut -d ' ' -f 1)
	local args=(--data-urlencode "u=$ND_USER" -d "t=$token" -d "s=$salt"
		-d v=1.16.1 -d c=releasomat-dev -d f=json)
	for param in "$@"; do
		args+=(--data-urlencode "$param")
	done
	curl -sS -G "$NAVIDROME/rest/$endpoint" "${args[@]}"
}

# json_value PATH: reads one value from a Subsonic JSON answer on stdin, e.g. "status" or
# "scanStatus.scanning". Lists are printed as their length (how many songs, …).
json_value() {
	python3 -c '
import json, sys
value = json.load(sys.stdin)["subsonic-response"]
for key in sys.argv[1].split("."):
    value = value.get(key, "") if isinstance(value, dict) else ""
if isinstance(value, bool):
    value = str(value).lower()
elif isinstance(value, list):
    value = len(value)
print(value)' "$1"
}

# --- 5. Admin user --------------------------------------------------------------------------------
if [[ $(subsonic ping | json_value status) == ok ]]; then
	say "Navidrome admin '$ND_USER' works"
else
	# A new Navidrome has no users: its first-run page creates the first admin. Same request here.
	say "Creating the Navidrome admin '$ND_USER'"
	answer=$(ND_USER=$ND_USER ND_PASSWORD=$ND_PASSWORD python3 -c '
import json, os
print(json.dumps({"username": os.environ["ND_USER"], "password": os.environ["ND_PASSWORD"]}))' |
		curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
			--data-binary @- "$NAVIDROME/auth/createAdmin")
	if [[ $answer == 403 ]]; then
		fail "Navidrome already has users, but the login in $ENV_FILE doesn't work.
       Put the old NAVIDROME_PASSWORD back, or start over with: scripts/dev_down.sh --reset"
	fi
	[[ $answer == 200 ]] || fail "Navidrome refused to create the admin (HTTP $answer)."
	[[ $(subsonic ping | json_value status) == ok ]] || fail "The new Navidrome admin can't log in."
fi

# --- 6. Scan --------------------------------------------------------------------------------------
# wait_for_scan: waits until Navidrome isn't scanning (at most 2 minutes).
wait_for_scan() {
	for _ in $(seq 120); do
		[[ $(subsonic getScanStatus | json_value scanStatus.scanning) == false ]] && return 0
		sleep 1
	done
	fail "Navidrome's scan didn't finish within 2 minutes."
}

say "Scanning the library"
wait_for_scan # a scan that is already running would ignore our request
[[ $(subsonic startScan | json_value status) == ok ]] || fail "Navidrome didn't start the scan."
wait_for_scan

# An empty search lists every song.
songs=$(subsonic search3 query= artistCount=0 albumCount=0 songCount=10000 |
	json_value searchResult3.song)

cat <<EOF

Dev environment is ready.
  Navidrome:       $NAVIDROME  (user '$ND_USER', password: NAVIDROME_PASSWORD in $ENV_FILE)
  Songs in it:     $songs (demo library: dev/library/Demo)
  Releasomat's music folder: dev/library/Releasomat (Navidrome sees it as "Releasomat")

Run Releasomat:   .venv/bin/uvicorn app.main:app --reload --port 8430 --env-file dev/.env
                  then open http://localhost:8430
Stop:             scripts/dev_down.sh
EOF
