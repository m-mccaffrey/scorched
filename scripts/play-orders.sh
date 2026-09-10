#!/usr/bin/env bash
# Launch Standing Orders, preferring the local virtual environment if present.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
[ -x ./.venv/bin/python ] && PYTHON=./.venv/bin/python
exec "$PYTHON" -m standing_orders "$@"
