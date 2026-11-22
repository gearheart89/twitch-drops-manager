# Build and publish the manager

The product/default branch is `manager`; `main` preserves upstream history. This
branch starts at upstream v2.6.0, commit `a9da17149776cdc4cdc7e6147c199ed4edc6da34`,
matching the working local image's engine revision.

## Builds

`Dockerfile` builds from this checkout's engine (`src/`, `web/`, `lang/`, `icons/`,
`main.py`) and `manager/`. The known dependency versions and noVNC assets come from
a pinned upstream image. Upstream/Python image digests are pinned. Genuine Chrome
is installed from Google's signed repository. Only Linux AMD64 is built.

`manager/adapt_upstream.py` applies asserted integration patches in the image.
When changing affected engine sections, update the assertions deliberately; failed
assertions stop incompatible builds. Do not run the script against your checkout.

```sh
docker build -t twitch-drops-manager:local .
sh .github/scripts/check-manager-image.sh twitch-drops-manager:local
```

This runs the manager test suite and offline Chrome fixtures for browser lifecycle,
global appearance, renewal feedback and password changes, all with synthetic data
and external networking disabled. This is focused manager validation, not the entire
upstream suite or live Twitch acceptance. Upstream tests remain in `tests/`; follow
the contribution policy for the checks relevant to engine changes.

Initialize the key, then for local Compose builds use:

```sh
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

For published images, use `docker compose pull` then `docker compose up -d`.
Windows users initialize keys with `Initialize-Secrets.ps1`. Linux users can run
the published image's `/manager/init_secrets.py` with a private key directory
mounted at `/output`, then provide that key as the Compose `account_key` secret.
For local Unraid builds, `sh unraid/build.sh` also refreshes icon caches. Local tags
do not receive registry update notices.

## GitHub Actions and GHCR

`manager-container.yml` verifies pushes/PRs targeting `manager`. PR verification
has read-only permissions. Publication runs only in this repository on `manager`,
after verification, with its exact built image tested again before pushing. The
registry credential is GitHub's automatic `GITHUB_TOKEN` with `packages: write`
only in the publishing job. No long-lived Docker Hub token is needed.

Successful publication creates:

- `ghcr.io/gearheart89/twitch-drops-manager:latest`
- `ghcr.io/gearheart89/twitch-drops-manager:sha-FULL_COMMIT_SHA`
- `ghcr.io/gearheart89/twitch-drops-manager:build-RUN_ID-RUN_ATTEMPT`

Use unique build tags or digests for exact rollback: a rerun can rebuild different
Chrome/OS packages and refresh the same revision tag, while its run/attempt build
tag is unique. Old reruns check the live branch head
and do not overwrite `latest` when a newer commit exists. Tags are pushed only
after tests for that exact image succeed. No scheduled rebuilds or automatic
Unraid installations are enabled.

GHCR packages are initially private. Make the package public under **Packages →
twitch-drops-manager → Package settings → Change visibility** for anonymous pulls.
Repository visibility alone does not make the package public. Fork workflows may
initially need enabling from Actions or `gh workflow enable manager-container.yml
--repo gearheart89/twitch-drops-manager`. Afterwards pushes to `manager` run the
workflow automatically; **Run workflow** supports manual runs.

The original workflows remain as inactive `.upstream` files for reference; they
are not dispatched to publish upstream releases/helpers. Use upstream's matching
v2.6.0 native helper.

## Safe publishing

Keep account storage, logs, screenshots, `.secrets/`, `.env`, `.dev-notes/`,
`.verification/` and `dist/` out of commits and Docker context. Only allowlisted
build inputs enter the image. Inspect `git diff --cached` before pushing. Preserve
MIT attribution and runtime package license notices. CI never requires account data.
