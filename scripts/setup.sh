#!/usr/bin/env bash
#
# Set Scorched up on Linux (including Raspberry Pi OS) or macOS.
#
# The game needs exactly one third-party package, pygame, and there are three
# reasonable ways to get it. This script picks the right one for the machine:
#
#   1. It is already installed  -> do nothing.
#   2. Debian/Raspberry Pi OS   -> apt install python3-pygame. This is the only
#      option that works on 32-bit Raspberry Pi OS, where no wheel exists, and
#      it sidesteps PEP 668 ("externally-managed-environment") entirely.
#   3. Anything else            -> a virtual environment in ./.venv.
#
# Re-running it is safe and cheap: it verifies rather than reinstalls.

set -euo pipefail

# pygame prints a greeting banner on import. Harmless when playing, but it
# lands in the middle of command substitutions here and corrupts what we
# report back to the user.
export PYGAME_HIDE_SUPPORT_PROMPT=1

MIN_PY_MAJOR=3
MIN_PY_MINOR=9
VENV_DIR=".venv"

MODE="auto"          # auto | apt | venv | system
WITH_DEV=0
ASSUME_YES=0
DRY_RUN=0

cd "$(dirname "$0")/.."

# -- output helpers ---------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    C_BOLD="$(tput bold)"; C_RED="$(tput setaf 1)"; C_GREEN="$(tput setaf 2)"
    C_YELLOW="$(tput setaf 3)"; C_OFF="$(tput sgr0)"
else
    C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_OFF=""
fi

step() { printf '%s==>%s %s\n' "$C_BOLD" "$C_OFF" "$*"; }
info() { printf '    %s\n' "$*"; }
good() { printf '    %s%s%s\n' "$C_GREEN" "$*" "$C_OFF"; }
warn() { printf '    %s%s%s\n' "$C_YELLOW" "$*" "$C_OFF" >&2; }
die()  { printf '\n%serror:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '    [dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

usage() {
    cat <<'USAGE'
Usage: scripts/setup.sh [options]

Installs everything Scorched needs, choosing the approach that suits this
machine. Safe to re-run.

Options:
  --venv        Always use a virtual environment in ./.venv
  --apt         Always use Debian's python3-pygame package
  --system      Always pip install into the system Python
  --dev         Also install the test tools (pytest, pyflakes)
  --yes, -y     Do not prompt; assume yes
  --dry-run     Print what would happen, change nothing
  --help, -h    Show this message

After setup, start the game with:  ./scripts/play.sh
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --venv)    MODE="venv" ;;
        --apt)     MODE="apt" ;;
        --system)  MODE="system" ;;
        --dev)     WITH_DEV=1 ;;
        -y|--yes)  ASSUME_YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1  (try --help)" ;;
    esac
    shift
done

confirm() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ "$DRY_RUN" -eq 1 ] && return 0
    [ -t 0 ] || return 0                 # non-interactive: proceed
    printf '    %s [Y/n] ' "$1"
    read -r reply || return 0
    case "$reply" in [nN]*) return 1 ;; *) return 0 ;; esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# pip against a slow or flaky link is the most likely thing to go wrong on a
# Pi over Wi-Fi, so give it room to retry and report the failure in a sentence
# rather than forty lines of urllib3 traceback.
pip_install() {
    if run "$PYTHON" -m pip install --quiet --retries 5 --timeout 30 "$@"; then
        return 0
    fi
    warn "the download failed (this is usually a slow or dropped connection)"
    info "retrying once..."
    if run "$PYTHON" -m pip install --quiet --retries 5 --timeout 60 "$@"; then
        return 0
    fi
    return 1
}

# -- 1. find a usable Python ------------------------------------------------
step "Looking for Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR} or newer"

PYTHON=""
for candidate in python3 python; do
    if have "$candidate" && "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    if have python3; then
        die "found $(python3 -V 2>&1), but Scorched needs ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+.
    On Debian or Raspberry Pi OS:  sudo apt install python3
    On macOS with Homebrew:        brew install python"
    fi
    die "no Python interpreter found.
    On Debian or Raspberry Pi OS:  sudo apt install python3
    On macOS with Homebrew:        brew install python"
fi
good "$("$PYTHON" -V 2>&1)  ($(command -v "$PYTHON"))"

# -- 2. decide how to install pygame ----------------------------------------
pygame_ok() {
    "$1" - <<'PY' 2>/dev/null
import sys
try:
    import pygame
except ImportError:
    sys.exit(1)
try:
    parts = tuple(int(p) for p in pygame.version.ver.split(".")[:2])
except (AttributeError, ValueError):
    sys.exit(0)          # unrecognisable version, but importable: allow it
sys.exit(0 if parts >= (2, 0) else 1)
PY
}

is_debian() { [ -f /etc/debian_version ] && have apt-get; }

apt_has_pygame() {
    have apt-cache && apt-cache show python3-pygame >/dev/null 2>&1
}

externally_managed() {
    "$PYTHON" - <<'PY' 2>/dev/null
import os, sys, sysconfig
sys.exit(0 if os.path.exists(
    os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)
PY
}

step "Checking for pygame"
if [ "$MODE" = "auto" ] && pygame_ok "$PYTHON"; then
    good "already present: pygame $("$PYTHON" -c 'import pygame; print(pygame.version.ver)' 2>/dev/null)"
    MODE="none"
elif [ "$MODE" = "auto" ]; then
    if is_debian && apt_has_pygame; then
        info "not installed; Debian's python3-pygame package is available"
        MODE="apt"
    else
        info "not installed; will use a virtual environment"
        MODE="venv"
    fi
fi

# -- 3. install -------------------------------------------------------------
case "$MODE" in
    none)
        ;;
    apt)
        step "Installing python3-pygame with apt"
        SUDO=""
        [ "$(id -u)" -ne 0 ] && SUDO="sudo"
        if ! confirm "Run ${SUDO:+$SUDO }apt-get install python3-pygame?"; then
            die "cancelled. Re-run with --venv to install without apt instead."
        fi
        if ! run ${SUDO:+$SUDO} apt-get install -y python3-pygame; then
            warn "apt failed; falling back to a virtual environment"
            MODE="venv"
        fi
        ;;
    system)
        step "Installing pygame into the system Python"
        if externally_managed; then
            warn "this Python is marked externally-managed (PEP 668);"
            warn "pip will refuse unless you pass --break-system-packages."
            warn "A virtual environment (--venv) is the safer choice here."
        fi
        run "$PYTHON" -m pip install --upgrade pip >/dev/null 2>&1 || true
        pip_install -r requirements.txt || die "could not install pygame.
    Try a virtual environment instead:  ./scripts/setup.sh --venv"
        ;;
esac

if [ "$MODE" = "venv" ]; then
    step "Creating a virtual environment in ./$VENV_DIR"
    if [ -x "$VENV_DIR/bin/python" ]; then
        info "reusing the existing environment"
    elif ! run "$PYTHON" -m venv "$VENV_DIR"; then
        die "could not create a virtual environment.
    On Debian or Raspberry Pi OS you may need:  sudo apt install python3-venv"
    fi
    if [ "$DRY_RUN" -eq 0 ]; then
        PYTHON="$VENV_DIR/bin/python"
    fi
    step "Installing pygame"
    info "this downloads about 12 MB and can take a minute on a Pi"
    run "$PYTHON" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    pip_install -r requirements.txt || die "could not download pygame.
    Check the network connection and try again, or install it from apt:
      sudo apt install python3-pygame && ./scripts/setup.sh"
fi

if [ "$WITH_DEV" -eq 1 ]; then
    step "Installing the test tools"
    pip_install pytest pyflakes ||
        warn "could not install the test tools; the game itself is unaffected"
fi

# -- 4. verify --------------------------------------------------------------
step "Verifying the installation"
if [ "$DRY_RUN" -eq 1 ]; then
    info "[dry-run] would import pygame and run the game's own self-check"
else
    # The dummy drivers let this work over SSH, on a headless Pi, and on a
    # machine with no sound card -- so a real problem is never masked by the
    # absence of a display.
    if ! SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy "$PYTHON" - <<'PY'; then
import sys
import pygame
pygame.init()
pygame.display.set_mode((320, 200))
print(f"    pygame {pygame.version.ver} works")
sys.exit(0)
PY
        die "pygame installed but could not start.
    On a minimal Linux install you may be missing SDL's runtime libraries:
      sudo apt install libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0"
    fi
    "$PYTHON" -m scorched --version >/dev/null || die "the game failed its own start-up check"
    good "scorched $("$PYTHON" -m scorched --version | awk '{print $2}') is ready"
fi

# -- 5. what next -----------------------------------------------------------
LAN_IP="$("$PYTHON" -c 'from scorched.discovery import local_addresses; print(local_addresses()[0])' 2>/dev/null || true)"

cat <<EOF

$C_GREEN$C_BOLD Setup complete.$C_OFF

  Play:              ./scripts/play.sh
  Headless server:   ./scripts/dedicated-server.sh --bots 2

  Hosting? Other players pick "Find LAN Games", or type your address:
      ${LAN_IP:-<the LAN address of this machine>}

  If the display feels sluggish on a Pi, try:
      ./scripts/play.sh --no-scale
EOF
