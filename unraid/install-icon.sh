#!/bin/sh
# Host-side only: install the bundled asset before Unraid needs to fetch it.
set -eu
cd "$(dirname "$0")/.."
container_name="${1:-twitch-drops-manager}"
case "$container_name" in
  ''|[!a-zA-Z0-9]*|*[!a-zA-Z0-9_.-]*)
    printf '%s\n' 'Invalid container name for icon installation.' >&2
    exit 1
    ;;
esac

umask 022
icon_source=manager/web/icon.png
test -s "$icon_source"
icon_directory=/boot/config/plugins/dockerMan/images
mkdir -p "$icon_directory"
cp "$icon_source" "$icon_directory/drops-manager.png"
chmod 644 "$icon_directory/drops-manager.png"

# Overwrite only this container's entries in both caches.
for cache in /var/lib/docker/unraid/images /var/local/emhttp/plugins/dynamix.docker.manager/images; do
  mkdir -p "$cache"
  cp "$icon_source" "$cache/$container_name-icon.png"
  chmod 644 "$cache/$container_name-icon.png"
done
printf 'Installed icon and refreshed Unraid caches for %s.\n' "$container_name"
