#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
storage_dir="${STORAGE_DIR:-/mnt/user/appdata/twitch-drops/data}"
secret_dir="${SECRET_DIR:-/mnt/user/appdata/twitch-drops-secrets}"
web_port="${WEB_PORT:-8080}"
image="${IMAGE:-ghcr.io/gearheart89/twitch-drops-manager:latest}"
umask 077
mkdir -p "$storage_dir" "$secret_dir"
chmod 700 "$storage_dir" "$secret_dir"
docker pull "$image"
sh unraid/install-icon.sh
docker run --rm --network none --entrypoint python \
  -v "$secret_dir:/output" "$image" /manager/init_secrets.py
docker run -d --name twitch-drops-manager --init --restart unless-stopped \
  --label net.unraid.docker.icon=file:///boot/config/plugins/dockerMan/images/drops-manager.png \
  --stop-timeout 90 --shm-size 512m \
  --tmpfs /runtime:rw,nosuid,nodev,mode=0700,size=512m \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777,size=1g \
  -p "$web_port:8080" -e TZ="${TZ:-America/Chicago}" \
  -v "$storage_dir:/data" \
  -v "$secret_dir/account-key:/run/secrets/account_key:ro" \
  "$image"
printf 'Dashboard ready on your Unraid server, port %s.\n' "$web_port"
printf 'Open the WebUI to create your dashboard password.\n'
printf 'Back up the encryption key separately from appdata. No secrets printed.\n'
