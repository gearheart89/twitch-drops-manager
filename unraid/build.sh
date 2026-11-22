#!/bin/sh
# Shared by fresh installs and manual upgrades; does not stop running accounts.
set -eu
cd "$(dirname "$0")/.."
docker build -t twitch-drops-manager:local .
sh unraid/install-icon.sh "${CONTAINER_NAME:-twitch-drops-manager}"
printf '%s\n' 'Image ready: twitch-drops-manager:local (local builds do not receive registry update notices)'
printf '%s\n' 'For an upgrade, select this Repository in Unraid and Apply with your existing mounts.'
