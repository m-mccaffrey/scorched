"""Interface colours shared by every game's front end.

Deliberately small and flat: a handful of greys, one accent, and semantic
colours for good/warn. Games layer their own world palettes on top -- terrain
ramps, team colours, explosion gradients -- but menus, panels and HUD chrome
look the same everywhere, so the collection feels like one thing.
"""

from __future__ import annotations

UI_BG = (18, 18, 26)
UI_PANEL = (32, 34, 48)
UI_PANEL_HI = (56, 60, 82)
UI_PANEL_LO = (12, 12, 20)
UI_TEXT = (222, 226, 238)
UI_DIM = (132, 138, 158)
UI_ACCENT = (248, 208, 64)
UI_WARN = (232, 84, 60)
UI_GOOD = (120, 220, 120)
UI_SHADOW = (8, 8, 12)

HP_GOOD = (96, 208, 88)
HP_FAIR = (248, 208, 64)
HP_POOR = (232, 84, 60)

#: Eight readable, clearly distinct player colours. A limited palette on
#: purpose -- it is what keeps the lo-fi look coherent.
TEAM_COLORS = (
    (232, 64, 48),    # red
    (72, 148, 255),   # blue
    (96, 208, 88),    # green
    (248, 208, 64),   # yellow
    (208, 96, 224),   # magenta
    (96, 224, 216),   # cyan
    (248, 152, 56),   # orange
    (216, 216, 216),  # white
)
COLOR_NAMES = ("Red", "Blue", "Green", "Yellow", "Magenta", "Cyan", "Orange", "White")


def shade(color, factor: float) -> tuple[int, int, int]:
    """Scale a colour's brightness, clamped. Used for bevels and shadows."""
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def health_color(fraction: float) -> tuple[int, int, int]:
    if fraction > 0.6:
        return HP_GOOD
    if fraction > 0.3:
        return HP_FAIR
    return HP_POOR


def team_color(index: int) -> tuple[int, int, int]:
    return TEAM_COLORS[index % len(TEAM_COLORS)]
