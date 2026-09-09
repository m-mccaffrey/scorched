#!/usr/bin/env bash
# Set Scorched up on Raspberry Pi OS (tested against Bookworm on a Pi 400).
#
# Bookworm marks the system Python as "externally managed", so pip refuses to
# install into it. This script prefers Debian's own pygame package and only
# falls back to a virtual environment if that package is unavailable.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Checking for pygame"
if python3 -c "import pygame" 2>/dev/null; then
    echo "    already installed: $(python3 -c 'import pygame; print(pygame.version.ver)')"
else
    echo "==> Installing python3-pygame from apt"
    if sudo apt-get install -y python3-pygame; then
        echo "    done"
    else
        echo "==> apt package unavailable; creating a virtual environment instead"
        python3 -m venv .venv
        ./.venv/bin/pip install --upgrade pip
        ./.venv/bin/pip install -r requirements.txt
        echo "    run the game with ./.venv/bin/python -m scorched"
    fi
fi

echo
echo "==> Ready. Start playing with:"
echo "      ./scripts/play.sh"
echo
echo "    If the display feels sluggish, try:"
echo "      ./scripts/play.sh --no-scale"
echo "      ./scripts/play.sh --fullscreen"
