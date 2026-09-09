#!/usr/bin/env bash
# Launch the game, preferring the local virtual environment when one exists.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
[ -x ./.venv/bin/python ] && PYTHON=./.venv/bin/python
exec "$PYTHON" -m scorched "$@"
