"""Entry point.

``python -m scorched``               play (menus, hosting, joining)
``python -m scorched server ...``    headless dedicated server
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "server":
        from .server import main as server_main
        return server_main(argv[1:])
    if argv and argv[0] in ("--version", "-V"):
        from . import __version__
        print(f"scorched {__version__}")
        return 0
    problem = _check_pygame()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    from .client.app import main as client_main
    return client_main(argv)


MIN_PYGAME = (2, 0, 0)


def _check_pygame() -> str | None:
    """Explain how to fix a missing or ancient pygame, rather than tracebacking.

    Only the client needs pygame; a dedicated server runs on a bare Python, so
    this check deliberately sits after the ``server`` dispatch above.
    """
    try:
        import pygame
    except ImportError:
        return (
            "Scorched needs pygame, which is not installed.\n"
            "\n"
            "  Windows:            py -3 -m pip install pygame-ce\n"
            "  Raspberry Pi OS:    sudo apt install python3-pygame\n"
            "  Other Linux/macOS:  python3 -m pip install pygame-ce\n"
        )
    try:
        version = tuple(int(part) for part in pygame.version.ver.split(".")[:3])
    except (AttributeError, ValueError):
        return None                      # unrecognisable, but let it try
    if version < MIN_PYGAME:
        return (
            f"Scorched needs pygame 2.0 or newer; this is {pygame.version.ver}.\n"
            "Upgrade with:  python3 -m pip install --upgrade pygame-ce\n"
        )
    return None


if __name__ == "__main__":
    raise SystemExit(main())
