#!/usr/bin/env bash
# Run a headless Standing Orders server. Needs no display and no pygame.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
[ -x ./.venv/bin/python ] && PYTHON=./.venv/bin/python
exec "$PYTHON" -m standing_orders server "$@"
