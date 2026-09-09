"""Colour choices.

The look is deliberately limited: a handful of flat colours, hard edges, no
gradients except a dithered sky.  That is what makes the original read as
"lo-fi" rather than merely "old" -- it never tried to be photographic, so it
still looks intentional forty years on.

Each round picks a sky and a ground scheme, so a five-round match feels like it
travels somewhere without ever leaving the palette.
"""

from __future__ import annotations

import random

# -- interface chrome --------------------------------------------------------
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
SHIELD = (110, 200, 255)

#: Sky schemes: (top, horizon, has_stars)
SKIES = (
    ((14, 12, 40), (196, 92, 62), True),      # dusk
    ((26, 44, 96), (150, 190, 226), False),   # clear day
    ((8, 8, 18), (44, 40, 88), True),         # night
    ((84, 34, 44), (232, 152, 72), False),    # mars
    ((30, 58, 62), (170, 196, 150), False),   # overcast green
    ((10, 26, 54), (226, 168, 96), True),     # early morning
)

#: Ground schemes: (crust, body, deep)
GROUNDS = (
    ((132, 100, 56), (94, 68, 38), (58, 42, 24)),     # earth
    ((150, 150, 158), (104, 104, 116), (62, 62, 72)), # stone
    ((196, 172, 108), (150, 126, 74), (96, 80, 46)),  # sand
    ((104, 146, 84), (70, 100, 56), (44, 62, 36)),    # turf
    ((176, 96, 84), (128, 62, 54), (80, 38, 34)),     # rust
    ((120, 128, 156), (82, 88, 112), (50, 54, 70)),   # slate
)

# -- explosion ramp: white hot to cold smoke --------------------------------
FIRE_RAMP = (
    (255, 255, 240), (255, 240, 160), (255, 196, 72), (248, 140, 40),
    (216, 84, 32), (156, 48, 28), (96, 32, 24), (56, 24, 22),
)
DIG_RAMP = (
    (206, 190, 150), (170, 148, 108), (132, 110, 74), (92, 76, 50),
    (64, 52, 34), (44, 36, 24),
)


def pick_scheme(seed: int) -> tuple[tuple, tuple]:
    """Deterministically choose a sky and ground pair for a round."""
    rng = random.Random(seed)
    return rng.choice(SKIES), rng.choice(GROUNDS)


def health_color(fraction: float) -> tuple[int, int, int]:
    if fraction > 0.6:
        return HP_GOOD
    if fraction > 0.3:
        return HP_FAIR
    return HP_POOR


def shade(color, factor: float) -> tuple[int, int, int]:
    """Scale a colour's brightness, clamped. Used for bevels and shadows."""
    return tuple(max(0, min(255, int(c * factor))) for c in color)
