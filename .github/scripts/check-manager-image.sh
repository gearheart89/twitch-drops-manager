#!/bin/sh
set -eu
image="${1:?Pass the built image tag}"
docker run --rm --network none --entrypoint python "$image" -m unittest discover -s /manager/tests -v
docker run --rm --init --network none --shm-size 256m \
  --tmpfs /runtime:rw,mode=0700 --tmpfs /tmp:rw,mode=1777 \
  --entrypoint python "$image" /manager/tests/chrome_local_check.py
for fixture in controls_ui_check password_ui_check; do
  docker run --rm --init --network none --shm-size 256m \
    --tmpfs /runtime:rw,mode=0700 --tmpfs /tmp:rw,mode=1777 \
    --entrypoint python "$image" "/manager/tests/$fixture.py" > "${RUNNER_TEMP:-/tmp}/$fixture.json"
done
