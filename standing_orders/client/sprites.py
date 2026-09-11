"""Pixel art for units, buildings and terrain detail.

Sprites are written as string art so they can be edited by hand in this file
rather than in an image editor -- at twelve pixels a side, a text grid *is* the
most convenient tool, and it keeps the game free of asset files.

Each glyph maps to a role, not a fixed colour, so one drawing serves all eight
team colours:

    .  transparent          1  team colour
    #  outline (team-tinted dark, so a unit reads as yours even in silhouette)
    2  team highlight       3  team shadow
    4  metal                5  metal shadow
    6  hot accent (muzzle, lamps, glow)
    7  near-black detail    8  neutral canvas (sandbags, cloth)

Built surfaces are cached per (sprite, colour, facing), so the whole roster
costs a few dozen small surfaces once at match start and nothing per frame.
"""

from __future__ import annotations

import pygame

from lanlib.theme import shade

METAL = (152, 156, 166)
METAL_DARK = (84, 88, 98)
ACCENT = (255, 226, 130)
INK = (26, 28, 34)
CANVAS = (168, 158, 130)

#: Units are drawn 12x12 inside a 14px tile; buildings fill the tile.
UNIT_SIZE = 12
BUILDING_SIZE = 14


# ---------------------------------------------------------------------------
# Units -- all drawn facing right; the builder mirrors them when needed.
# ---------------------------------------------------------------------------

# A light recon buggy: long, low and mostly wheels, so it reads as quick next
# to the Bruiser's slab. The first attempt was a tall blob and looked like a
# small tank, which is precisely the wrong thing for the fastest unit.
SCOUT = [
    "............",
    "............",
    ".......##...",
    "......#22#..",
    "......#11#..",
    "..#####11#..",
    ".#211111111#",
    ".##########.",
    "..#4#..#4#..",
    ".#454##454#.",
    "..#4#..#4#..",
    "............",
]

TROOPER = [
    "....####....",
    "...#2222#...",
    "...#2117#...",
    "....####....",
    "..#5#11#....",
    ".#455#11#...",
    "..#5#1111#..",
    "....#111#...",
    "....#1#1#...",
    "....#3#3#...",
    "...##...##..",
    "............",
]

GUNNER = [
    "............",
    "...####.....",
    "..#2222#....",
    "..#2117#....",
    "...####.....",
    "..#1111#....",
    ".#111111#...",
    ".#11#5555555",
    ".#1#.#444446",
    "..#..#5555#.",
    "..##..##....",
    "............",
]

BRUISER = [
    "............",
    "......###...",
    ".....#221#..",
    "....#22111#.",
    "..###111#5#.",
    ".#2111111#5#",
    ".#111111111#",
    ".#111111111#",
    ".###########",
    ".#5454545#..",
    "#455555554#.",
    ".#4#4#4#4#..",
]

# Short, stocky, hard hat and a wrench -- deliberately unlike the Trooper's
# upright rifleman silhouette so you never mistake your economy for your army.
WORKER = [
    "............",
    "....####....",
    "...#6666#...",
    "...######...",
    "....#22#....",
    "..#4#11#....",
    ".#44#111#...",
    "..#4#1111#..",
    "....#111#...",
    "....#1#1#...",
    "...##...##..",
    "............",
]

UNIT_ART = {
    "worker": WORKER,
    "scout": SCOUT,
    "trooper": TROOPER,
    "ranged": GUNNER,
    "bruiser": BRUISER,
}


# ---------------------------------------------------------------------------
# Buildings -- 14x14, filling the tile.
# ---------------------------------------------------------------------------

# A bunker with a radio mast, a window band and sandbags at the door.
BASE = [
    "......6.......",
    "......4.......",
    "....##4##.....",
    "...#22222#....",
    "..#2222222#...",
    ".#211111111#..",
    ".#17777777 1#.",
    ".#17777777 1#.",
    ".#1111111111#.",
    ".#1111111111#.",
    ".#8##4444##8#.",
    ".#8##4554##8#.",
    ".############.",
    "..#5######5#..",
]

# A hut with a symmetric pitched roof and a canvas door. The first attempt had
# one eave overhanging further than the other and read as broken rather than
# rustic.
BARRACKS = [
    "..............",
    "......##......",
    ".....#22#.....",
    "....#2222#....",
    "...#222222#...",
    "..#22222222#..",
    ".#2222222222#.",
    "#111111111111#",
    "#1#8888#11111#",
    "#1#8778#11111#",
    "#1#8778#11111#",
    "#1#8778#11111#",
    "##############",
    "..............",
]

# A warehouse with a loading door and crates stacked outside.
DEPOT = [
    "..............",
    "...########...",
    "..#22222222#..",
    ".#2111111112#.",
    ".#1111111111#.",
    ".#1#8888##11#.",
    ".#1#8778##11#.",
    ".#1#8778##11#.",
    ".#1#8778##11#.",
    ".############.",
    "..#666#..#66#.",
    "..#616#..#61#.",
    "..#####..####.",
    "..............",
]

# A watchtower: legs, a platform and a gun under a roof.
TOWER = [
    "......##......",
    ".....#22#.....",
    "....#2222#....",
    "...##2222##...",
    "..#21111112#..",
    "..#1#7777#1#..",
    "..#1#7667#1#..",
    "..#11111111#..",
    "..##########..",
    "...#4####4#...",
    "...#4#..#4#...",
    "..#54#..#45#..",
    "..#4#....#4#..",
    "..#5#....#5#..",
]

# Stacked blocks with a rubble course, so a barricade reads as built, not grown.
WALL = [
    "..............",
    ".############.",
    ".#1144551144#.",
    ".#1##44##111#.",
    ".#5544554455#.",
    ".############.",
    ".#44551144554.",
    ".#4##1##44##1.",
    ".#5544554455#.",
    ".############.",
    ".#1144551144#.",
    ".#11##4444##1.",
    ".############.",
    "..............",
]

# A field hospital: a canvas tent with the flaps open and a cross on the roof.
# Deliberately the only white-and-cloth structure on the board, so "the place
# the wounded go" reads at a glance without anybody hovering it.
MEDIC = [
    "..............",
    "......##......",
    ".....#88#.....",
    "....#8886#....",
    "...#88666#....",
    "..#888666888#.",
    "..#886666688#.",
    ".#8886668888#.",
    ".#8888888888#.",
    ".#88#8888#88#.",
    ".#88#8118#88#.",
    ".#88#8118#88#.",
    ".############.",
    "..............",
]

# An airfield: a strip with a marked threshold and a small aircraft on it.
AIRFIELD = [
    "..............",
    ".############.",
    ".#7777777777#.",
    ".#7#4#7#4#77#.",
    ".#77777777#7#.",
    ".#777#11#777#.",
    ".#77#1111#77#.",
    ".#7#111111#7#.",
    ".#77##11##77#.",
    ".#777#11#777#.",
    ".#77777777#7#.",
    ".#7#4#7#4#77#.",
    ".############.",
    "..............",
]

BUILDING_ART = {
    "medic": MEDIC,
    "airfield": AIRFIELD,
    "depot": DEPOT,
    "tower": TOWER,
    "wall": WALL,
    "base": BASE,
    "barracks": BARRACKS,
}


# ---------------------------------------------------------------------------
# A resource node: a little stack of supply crates, tinted by its owner.
# ---------------------------------------------------------------------------

NODE = [
    "..............",
    "..............",
    "....######....",
    "...#822228#...",
    "...#811118#...",
    "..########....",
    "..#8222228#...",
    "..#8111118#...",
    "..#8111118#...",
    "..#########...",
    "..............",
    "..............",
]


def palette_for(colour) -> dict:
    """Map the glyph roles onto one team's colours."""
    return {
        "1": colour,
        "2": shade(colour, 1.45),
        "3": shade(colour, 0.62),
        # The outline is tinted rather than pure black so a unit still reads as
        # yours when it is little more than a silhouette.
        "#": shade(colour, 0.28),
        "4": METAL,
        "5": METAL_DARK,
        "6": ACCENT,
        "7": INK,
        "8": CANVAS,
    }


def build(art: list, colour, flip: bool = False,
          dim: float = 1.0) -> pygame.Surface:
    """Render one string-art sprite for a given team colour."""
    palette = palette_for(colour)
    if dim != 1.0:
        palette = {key: shade(value, dim) for key, value in palette.items()}
    height = len(art)
    width = max(len(row) for row in art)
    surface = pygame.Surface((width, height), pygame.SRCALPHA)
    for y, row in enumerate(art):
        for x, glyph in enumerate(row):
            if glyph == ".":
                continue
            rgb = palette.get(glyph)
            if rgb is not None:
                surface.set_at((x, y), rgb)
    if flip:
        surface = pygame.transform.flip(surface, True, False)
    return surface


class SpriteBank:
    """Every sprite the match needs, built once and cached."""

    def __init__(self) -> None:
        self._cache: dict = {}

    def unit(self, code: str, colour, flip: bool = False,
             ghost: bool = False) -> pygame.Surface:
        key = ("u", code, tuple(colour), flip, ghost)
        got = self._cache.get(key)
        if got is None:
            got = build(UNIT_ART[code], colour, flip, 0.42 if ghost else 1.0)
            self._cache[key] = got
        return got

    def building(self, code: str, colour, under: bool = False) -> pygame.Surface:
        key = ("b", code, tuple(colour), under)
        got = self._cache.get(key)
        if got is None:
            got = build(BUILDING_ART[code], colour, dim=0.5 if under else 1.0)
            self._cache[key] = got
        return got

    def node(self, colour) -> pygame.Surface:
        key = ("n", tuple(colour))
        got = self._cache.get(key)
        if got is None:
            got = build(NODE, colour)
            self._cache[key] = got
        return got

    def clear(self) -> None:
        self._cache.clear()
