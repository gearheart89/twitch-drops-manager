# Deploy Twitch Drops Manager

The published image is `ghcr.io/gearheart89/twitch-drops-manager:latest` (AMD64 only).
It includes genuine Google Chrome for server-side verification and session renewal.
One port, 8080, serves all accounts. No Docker socket or privileged mode is required.

## Upgrade the existing Unraid installation

1. Ensure the [Manager container workflow](https://github.com/gearheart89/twitch-drops-manager/actions/workflows/manager-container.yml)
   has successfully published the image. The GHCR package must be public for
   anonymous pulls; its visibility is separate from the public repository.
2. In Unraid, normally **Stop** `twitch-drops-manager` to checkpoint account data.
3. **Edit** the container and change **Repository** to
   `ghcr.io/gearheart89/twitch-drops-manager:latest`.
4. Keep all existing data, encryption-key and optional legacy password-file mounts,
   ports, timezone, tmpfs and shared-memory settings. Do not regenerate the key or
   delete account data. Both `/runtime` and `/tmp` must stay tmpfs.
5. In Advanced View, set **Icon URL** to
   `https://raw.githubusercontent.com/gearheart89/twitch-drops-manager/manager/manager/web/icon.png`.
   Your already-installed cached D icon can also remain in place.
6. Apply to recreate the container, unlock with the same WebUI password, and verify
   progress and renewal. Valid saved Twitch sessions are restored.

Afterwards use **Check for Updates**, then **Update**. Unraid checks published images,
not source commits. Allow time for the workflow and the next check. Scheduled checks
or installations depend on your Unraid configuration; this repository does not enable
them. Local tags such as `chrome-round2`, `controls-round3` and `local` do not track GHCR.
To roll back, select a previous `build-RUN_ID-RUN_ATTEMPT` tag or image digest with the same mounts. This first
registry build retains the working local build's session/encryption formats.

## Fresh Unraid setup

Download/extract the source for the `manager` branch to a source directory separate
from account data, then run in the Unraid terminal:

```sh
cd /mnt/user/appdata/twitch-drops-source
sh unraid/install.sh
```

The installer pulls the image, installs the icon and refreshes this container's two
icon caches, initializes the key if absent, and starts one container. It does not
replace an existing container. Open `http://YOUR-UNRAID-IP:8080` and create a WebUI
password of at least 16 characters.

| Host | Container | Purpose |
| --- | --- | --- |
| `/mnt/user/appdata/twitch-drops/data` | `/data` | Encrypted account data and settings |
| `/mnt/user/appdata/twitch-drops-secrets/account-key` | `/run/secrets/account_key` | Separate read-only encryption key |

Customize `STORAGE_DIR`, `SECRET_DIR`, `WEB_PORT` and `TZ` before installation.
For editable Docker-tab management, use `unraid/my-twitch-drops-manager.xml` as the
user template after pulling the image and initializing the key. It supplies the
mounts, tmpfs, shared memory and stop grace period. The script-created container
can be managed through Docker CLI; use the XML template for editable Docker-tab setup.

To repair stale icons without restarting mining, run `sh unraid/install-icon.sh`
from the extracted source and hard-refresh the Docker page. This runs automatically
on fresh install/local build. A renamed container can be passed as the first argument.
Public GitHub icon hosting avoids depending on app readiness.

## Sign-in and account controls

Create the manager password, add named accounts, sign in, and select linked games
in **Settings → Games**. Each account has an independent session and game list.
Only one desktop-helper login window is active at a time; mining remains concurrent.

If embedded Chrome is rejected, select **Use desktop helper** and download the
matching [upstream v2.6.0 helper](https://github.com/rangermix/TwitchDropsMiner/releases/tag/v2.6.0).
Extract/run it on your PC and give it the full root URL with scheme and port,
without `/accounts/...`. Complete Twitch login/MFA, close its temporary browser
normally, and leave the helper running until success. It is not needed for later
renewal after successful server verification.

**Priority list only** restricts an account to its chosen games. Periodic inventory
refresh discovers future campaigns. The manager Theme button controls all account
views. **Change password** requires the current password and revokes other WebUI sessions.

**Renew now** queues server renewal for the selected account. Queued/running is not
success. A successful result shows a timestamp, increased proof generation and later
expiry after validation and saving. Last-success details cover the current worker
lifetime; generation/expiry survive restart. Requests are limited to once a minute.
Fixed failure codes are displayed and existing engine retry behavior remains.

**Pause** stops watching for that account; earned rewards can still be claimed.
Its pause state resets on worker/container restart. **Stop account** preserves
encrypted account data. The outer browser tab can close while mining continues.

**Delete account** requires typing its exact manager name. It stops its worker,
revokes any helper login, removes runtime files and the encrypted account record,
and removes the registry entry. Other accounts and the Twitch account itself are
unaffected. Existing backups can still contain older records. If cleanup fails,
the stopped account remains marked **deletion pending**; retry Delete account to
finish. Pending deletions cannot be restarted or checkpointed.

## Storage and backup

Keep the same encryption key across updates. Losing it makes saved accounts
unrecoverable. Account files, registry, preferences and salted password hashes are
encrypted at rest. Plaintext worker data/profiles live in tmpfs; checkpoints save
approximately every 15 seconds and after normal shutdown. Forced stops can lose
changes since the last checkpoint.

Keep the key directory mode 0700 and key file mode 0600. Do not share it over SMB
or include it in the same general backup as encrypted appdata. Stop normally for
a consistent backup. Host administrators or anyone with both data and key can
decrypt sessions; swap/disk protection is a separate host responsibility.

Legacy `/run/secrets/dashboard_password` mounts remain supported. A changed password's
encrypted hash takes precedence after restart. Password changes do not rotate the
key or log Twitch accounts out. Use a trusted LAN/VPN or HTTPS reverse proxy;
HTTP does not encrypt passwords in transit. For a proxy set `PUBLIC_BASE_URL`,
preserve Host/WebSocket upgrades, and allow custom DNS names with `ALLOWED_HOSTS`.
Never publish keys, appdata, cookies, logs or unredacted account screenshots.
