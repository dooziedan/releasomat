#!/usr/bin/env bash
# Stops the dev services from docker-compose.dev.yml (Navidrome, and Releasomat if running).
#
#   scripts/dev_down.sh                # stop; all data stays, scripts/dev_up.sh starts it again
#   scripts/dev_down.sh --reset        # stop and start over (asks first):
#                                      # deletes the dev Navidrome's database, Releasomat's dev
#                                      # databases and the music Releasomat downloaded in dev
#   scripts/dev_down.sh --reset --yes  # the same without asking
#
# --reset keeps dev/.env (so the passwords stay the same), cookies.txt and the demo library. It only ever deletes inside this checkout's dev/ folder.
set -euo pipefail

cd "$(dirname "$0")/.."  # the Releasomat folder, wherever the script is started from

reset=false
ask=true
for arg in "$@"; do
	case $arg in
	--reset) reset=true ;;
	--yes | -y) ask=false ;;
	-h | --help)
		sed -n '2,10s/^# \{0,1\}//p' "$0"
		exit 0
		;;
	*)
		echo "Unknown option: $arg (see --help)" >&2
		exit 2
		;;
	esac
done

echo "==> Stopping the dev services"
docker compose -f docker-compose.dev.yml --profile app down --remove-orphans

$reset || exit 0

# Safety: dev/ must be a real folder inside this checkout, not a link to somewhere else.
dev=$(pwd -P)/dev
if [[ -L dev || ! -d dev ]]; then
	echo "dev/ is missing or a link; nothing deleted." >&2
	exit 1
fi

# Folders whose contents --reset deletes (the folders themselves stay). A cookies.txt for
# YouTube in the config folders is kept.
folders=(dev/navidrome-data dev/config dev/config-docker dev/library/releasomat)

for folder in "${folders[@]}"; do
	if [[ -L $folder ]] || [[ -e $folder && $(realpath "$folder") != "$dev"/* ]]; then
		echo "$folder is a link or outside dev/; nothing deleted." >&2
		exit 1
	fi
done

# to_delete FOLDER: lists the files and folders --reset deletes directly inside FOLDER.
to_delete() {
	if [[ $1 == dev/config* ]]; then
		find "$1" -mindepth 1 -maxdepth 1 ! -name cookies.txt "${@:2}"
	else
		find "$1" -mindepth 1 -maxdepth 1 "${@:2}"
	fi
}

echo
echo "This deletes everything in:"
for folder in "${folders[@]}"; do
	if [[ -d $folder ]]; then
		files=$(find "$folder" -type f ! -path "dev/config*/cookies.txt" | wc -l)
		printf '  %-28s %s file(s), %s\n' "$folder/" "$files" "$(du -sh "$folder" | cut -f 1)"
	fi
done
echo "Kept: dev/.env, cookies.txt, dev/library/Demo, dev/slskd, dev/screenshots"
echo

if $ask; then
	if [[ ! -t 0 ]]; then
		echo "Not deleted: add --yes to delete without asking." >&2
		exit 1
	fi
	read -r -p "Type yes to delete: " answer
	if [[ $answer != yes ]]; then
		echo "Nothing deleted."
		exit 1
	fi
fi

for folder in "${folders[@]}"; do
	mkdir -p -- "$folder"
	if ! to_delete "$folder" -exec rm -rf -- {} +; then
		echo "Some files in $folder belong to another user (a container made them)." >&2
		echo "Delete them with: docker run --rm -v \"\$PWD/dev:/devdir\" alpine \\" >&2
		echo "  sh -c 'rm -rf /devdir/${folder#dev/}/*'" >&2
		exit 1
	fi
done

echo "Done. Start again with: scripts/dev_up.sh"
