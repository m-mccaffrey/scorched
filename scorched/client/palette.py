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

# Interface chrome is shared across every game in the repo; re-exported here so
# Scorched's own modules have a single place to import colour from.
from lanlib.theme import (HP_FAIR, HP_GOOD, HP_POOR, UI_ACCENT, UI_BG, UI_DIM,
                          UI_GOOD, UI_PANEL, UI_PANEL_HI, UI_PANEL_LO,
                          UI_SHADOW, UI_TEXT, UI_WARN, health_color, shade)

SHIELD = (110, 200, 255)

#: Re-exported so the rest of the game imports every colour from one module.
__all__ = [
    "HP_FAIR", "HP_GOOD", "HP_POOR", "UI_ACCENT", "UI_BG", "UI_DIM", "UI_GOOD",
    "UI_PANEL", "UI_PANEL_HI", "UI_PANEL_LO", "UI_SHADOW", "UI_TEXT", "UI_WARN",
    "health_color", "shade", "SHIELD", "SKIES", "GROUNDS", "FIRE_RAMP",
    "DIG_RAMP", "pick_scheme",
]

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


