"""Entry point.

``python -m standing_orders``               play
``python -m standing_orders server ...``    headless dedicated server
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
        print(f"standing-orders {__version__}")
        return 0
    problem = _check_pygame()
    if problem:
        print(problem, file=sys.stderr)
        return 1
    from .client.app import main as client_main
    return client_main(argv)


def _check_pygame() -> str | None:
    """Explain how to fix a missing pygame rather than tracebacking.

    Only the client needs it; a dedicated server runs on a bare Python, which
    is why this sits after the ``server`` dispatch above.
    """
    try:
        import pygame
    except ImportError:
        return (
            "Standing Orders needs pygame, which is not installed.\n"
            "\n"
            "  Windows:            py -3 -m pip install pygame-ce\n"
            "  Raspberry Pi OS:    sudo apt install python3-pygame\n"
            "  Other Linux/macOS:  python3 -m pip install pygame-ce\n"
        )
    try:
        version = tuple(int(p) for p in pygame.version.ver.split(".")[:3])
    except (AttributeError, ValueError):
        return None
    if version < (2, 0, 0):
        return (f"Standing Orders needs pygame 2.0+; this is "
                f"{pygame.version.ver}.\n")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
