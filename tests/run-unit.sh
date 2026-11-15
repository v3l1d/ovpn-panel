#!/usr/bin/env bash
# Unit and security tests in a clean Debian 12 / Python 3.11 container (the versions the product runs on).
# Needs Docker, nothing else. The same suite runs in CI with plain pytest.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite container paths
SRC="$(pwd -W 2>/dev/null || pwd)"
exec docker run --rm -v "$SRC:/src:ro" python:3.11-slim bash -c '
  set -e
  apt-get update -qq && apt-get install -y -qq --no-install-recommends openssl >/dev/null
  cp -r /src /work && cd /work
  pip install -q --disable-pip-version-check --require-hashes -r app/requirements.lock
  pip install -q --disable-pip-version-check -r tests/requirements-dev.txt
  python -m pytest -q -p no:cacheprovider "$@"
' _ "$@"
