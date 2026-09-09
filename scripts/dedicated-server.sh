#!/usr/bin/env bash
# Run a headless server. Useful when the Pi should host but not play, or when
# you want the match to survive everyone closing their client.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
[ -x ./.venv/bin/python ] && PYTHON=./.venv/bin/python
exec "$PYTHON" -m scorched server "$@"
