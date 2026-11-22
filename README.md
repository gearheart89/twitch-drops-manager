# Twitch Drops Manager

One WebUI and Docker container for multiple independent Twitch accounts. Each
account has its own games, priorities, session and reward history. Accounts can
progress the same campaign concurrently; each account watches one stream at a time.

This personal fork of [rangermix/TwitchDropsMiner](https://github.com/rangermix/TwitchDropsMiner)
adds encrypted account storage, first-run WebUI password setup, password changes,
global Dark/Light appearance, manual session renewal and Unraid deployment files.
The engine is based on upstream v2.6.0. See [credits and license notices](THIRD_PARTY_NOTICES.md).

## Install

- **Unraid:** follow [setup and migration](docs/manager-deployment.md). Existing
  users retain their data/key mounts and switch the Repository to
  `ghcr.io/gearheart89/twitch-drops-manager:latest` after the first image is published.
- **Docker Compose:** run `./Initialize-Secrets.ps1` on Windows, then
  `docker compose pull` and `docker compose up -d`. Open `http://localhost:8080`
  and create your WebUI password. [Linux and source builds](docs/manager-development.md).

Add an account, sign in, and choose its games in **Settings → Games**. If Twitch
rejects the embedded browser, use the matching upstream v2.6.0 desktop helper.
Provide the full dashboard URL including `http://` or `https://` and the port.
[Login and account management](docs/manager-deployment.md#sign-in-and-account-controls).

## Updates

Push changes to this fork's **manager** branch. GitHub Actions builds and tests the
image, then publishes `latest` and a unique build tag to GHCR. Once Unraid uses
that registry Repository, **Check for Updates** can detect a changed image and
**Update** installs it. Publication must succeed before an update is available.
This setup does not automatically install updates.

See [development and publishing](docs/manager-development.md),
[deployment and backup](docs/manager-deployment.md), and the
[upstream user guide](docs/usage.md). The original [upstream README](UPSTREAM_README.md)
and license remain available. The upstream branch is preserved as `main`; this
fork's default product branch is `manager`.

Use on your own hardware with intended accounts. Twitch can reject sessions or
campaign access; linked game accounts and eligible campaigns/channels are required.
