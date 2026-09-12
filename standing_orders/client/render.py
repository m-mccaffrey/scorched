"""Drawing the battlefield.

The terrain never changes during a match, so it is composited once into a
single surface and blitted whole -- one opaque blit per frame instead of eight
hundred tile draws. Everything that does move (units, fog, orders, effects) is
a few dozen small draws on top.

Sprites are shapes rather than pictures on purpose: at fourteen pixels a tile,
a circle that is obviously not a square reads instantly, and a detailed little
soldier reads as mud.
"""

from __future__ import annotations

import pygame

from lanlib import ui
from lanlib.theme import (UI_ACCENT, UI_BG, UI_DIM, UI_PANEL, UI_PANEL_HI,
                          UI_PANEL_LO, UI_TEXT, UI_WARN, health_color, shade,
                          team_color)

from ..grid import FOREST, ROCK, WATER
from ..units import BUILDING, UNIT
from .sprites import SpriteBank

TILE = 14
#: Terrain is composited in squares this many tiles on a side, and this many
#: squares are kept at once. A viewport spans at most four, so panning never
#: repaints and memory does not care how big the world is.
PATCH = 32
PATCH_CACHE = 12
SCREEN_W, SCREEN_H = 640, 400
TOP_H = 16
HUD_H = 44
PANEL_W = 188
BOARD_W = SCREEN_W - PANEL_W - 4
BOARD_H = SCREEN_H - TOP_H - HUD_H

# Terrain colours: muted, so unit colours are the only saturated thing on
# screen and your army is never hard to find.
C_OPEN = (58, 66, 52)
C_OPEN_ALT = (54, 62, 48)
C_FOREST = (34, 54, 36)
C_FOREST_TOP = (46, 72, 46)
C_ROCK = (72, 70, 78)
C_ROCK_TOP = (98, 96, 104)
C_WATER = (34, 52, 84)
C_WATER_TOP = (48, 72, 110)
C_NODE = (176, 152, 64)
C_TUFT = (74, 86, 62)
C_TUFT_DRY = (86, 92, 60)
C_TRUNK = (40, 34, 26)
C_ROCK_FACE = (86, 84, 92)

#: Terrain as the minimap sees it: one flat colour a tile, no detail at all.
MINIMAP_TERRAIN = {
    ROCK: C_ROCK,
    WATER: C_WATER,
    FOREST: C_FOREST,
}

#: Explored-but-unseen ground is dimmed; never-explored ground is blacked out.
FOG_SEEN_ALPHA = 130
FOG_UNKNOWN_ALPHA = 246


class Board:
    """Maps tile coordinates to screen pixels and back, through a camera.

    Maps used to be capped at exactly the size of the viewport, so there was no
    camera at all and ``ox``/``oy`` were constants that centred a small map.
    They are now derived from a scroll position, which is why every caller --
    units, order paths, highlights, tooltips, tracers, drifting numbers -- kept
    working untouched: they all ask the board where a tile is, and the board
    now answers with the camera in mind.

    A map that fits on screen still centres and never scrolls, so the three
    original maps draw exactly as they always did.
    """

    #: Screen rectangle the battlefield is drawn into.
    VIEW = pygame.Rect(0, TOP_H, BOARD_W, BOARD_H)

    def __init__(self, tilemap) -> None:
        self.map = tilemap
        self.pixel_w = tilemap.width * TILE
        self.pixel_h = tilemap.height * TILE
        # Centre a map smaller than the viewport rather than jamming it into
        # the corner; a larger one starts at the top left and scrolls.
        self.pad_x = max(0, (BOARD_W - self.pixel_w) // 2)
        self.pad_y = max(0, (BOARD_H - self.pixel_h) // 2)
        self.cam_x = 0
        self.cam_y = 0

    # -- camera ------------------------------------------------------------
    @property
    def scrolls(self) -> bool:
        return self.pixel_w > BOARD_W or self.pixel_h > BOARD_H

    @property
    def max_cam(self) -> tuple[int, int]:
        return (max(0, self.pixel_w - BOARD_W), max(0, self.pixel_h - BOARD_H))

    @property
    def ox(self) -> int:
        return self.pad_x - self.cam_x

    @property
    def oy(self) -> int:
        return TOP_H + self.pad_y - self.cam_y

    def scroll_by(self, dx: int, dy: int) -> None:
        limit_x, limit_y = self.max_cam
        self.cam_x = min(limit_x, max(0, self.cam_x + int(dx)))
        self.cam_y = min(limit_y, max(0, self.cam_y + int(dy)))

    def centre_on(self, tile) -> None:
        """Put a tile in the middle of the viewport, as far as the edges allow."""
        self.cam_x = self.cam_y = 0
        self.scroll_by(tile[0] * TILE + TILE // 2 - BOARD_W // 2,
                       tile[1] * TILE + TILE // 2 - BOARD_H // 2)

    def camera_tiles(self) -> pygame.Rect:
        """The tile rectangle currently on screen, as a minimap needs it."""
        return pygame.Rect(self.cam_x // TILE, self.cam_y // TILE,
                           min(self.map.width, BOARD_W // TILE + 1),
                           min(self.map.height, BOARD_H // TILE + 1))

    def on_screen(self, tile) -> bool:
        """Is this tile worth drawing? Off-camera work is wasted on a Pi."""
        x, y = self.to_screen(tile)
        return (-TILE < x - self.VIEW.x < BOARD_W
                and -TILE < y - self.VIEW.y < BOARD_H)

    def to_screen(self, tile) -> tuple[int, int]:
        return (self.ox + tile[0] * TILE, self.oy + tile[1] * TILE)

    def centre(self, tile) -> tuple[int, int]:
        x, y = self.to_screen(tile)
        return (x + TILE // 2, y + TILE // 2)

    def to_tile(self, pos) -> tuple[int, int] | None:
        if not self.VIEW.collidepoint(pos):
            return None        # a click on the panel is not a click on a tile
        tx = (pos[0] - self.ox) // TILE
        ty = (pos[1] - self.oy) // TILE
        if 0 <= tx < self.map.width and 0 <= ty < self.map.height:
            return (int(tx), int(ty))
        return None

    def rect(self, tile) -> pygame.Rect:
        x, y = self.to_screen(tile)
        return pygame.Rect(x, y, TILE, TILE)


class Renderer:
    def __init__(self) -> None:
        self.board: Board | None = None
        self.fog = pygame.Surface((1, 1), pygame.SRCALPHA)
        self._patches: dict = {}
        self._patch_order: list = []
        self._fog_key = None
        self._minimap: pygame.Surface | None = None
        self._shaded: pygame.Surface | None = None
        self._shade_key = None
        self.sprites = SpriteBank()

    # -- setup -------------------------------------------------------------
    def begin_match(self, tilemap) -> None:
        self.sprites.clear()
        self.board = Board(tilemap)
        # Terrain is painted a patch at a time, on demand. Compositing a whole
        # world up front cost 45MB of surface and 435ms on a 224x128 map, and
        # would have been 257MB on a world four times that -- for a viewport
        # that can only ever show 32x24 tiles of it. Cost now follows the
        # window rather than the world.
        self._patches: dict = {}
        self._patch_order: list = []
        self.fog = pygame.Surface((BOARD_W, BOARD_H), pygame.SRCALPHA)
        self._fog_key = None
        self._minimap = None
        self._shaded = None
        self._shade_key = None

    # -- terrain patches ----------------------------------------------------
    def _patch(self, px: int, py: int) -> pygame.Surface:
        """One PATCH x PATCH square of terrain, painted once and kept."""
        key = (px, py)
        got = self._patches.get(key)
        if got is None:
            got = pygame.Surface((PATCH * TILE, PATCH * TILE)).convert()
            got.fill(UI_BG)
            self._paint_terrain(got, px * PATCH, py * PATCH, PATCH, PATCH)
            self._patches[key] = got
            self._patch_order.append(key)
            # A hard ceiling on how much terrain is held at once. Twelve
            # patches is comfortably more than the four a viewport can span,
            # so panning back and forth never repaints, and the bill is the
            # same on the largest world as on the smallest.
            while len(self._patch_order) > PATCH_CACHE:
                self._patches.pop(self._patch_order.pop(0), None)
        return got

    def _paint_terrain(self, surface, x0: int = 0, y0: int = 0,
                       width: int = 0, height: int = 0) -> None:
        """Composite a rectangle of tiles into a surface.

        Terrain never changes, so every tile can afford a little hand-placed
        detail -- tufts, ripples, rock facets -- picked from a hash of its
        coordinates. The variation is what stops a big grid of flat squares
        reading as a spreadsheet, and it costs nothing per frame.

        Painted a patch at a time rather than all at once: a whole world is far
        too much surface to hold, and almost none of it is on screen.
        """
        tilemap = self.board.map
        width = width or tilemap.width
        height = height or tilemap.height
        for y in range(y0, min(tilemap.height, y0 + height)):
            for x in range(x0, min(tilemap.width, x0 + width)):
                char = tilemap.at(x, y)
                rect = pygame.Rect((x - x0) * TILE, (y - y0) * TILE, TILE, TILE)
                noise = _tile_hash(x, y)
                if char == ROCK:
                    self._paint_rock(surface, rect, noise)
                elif char == WATER:
                    self._paint_water(surface, rect, noise)
                elif char == FOREST:
                    self._paint_forest(surface, rect, noise)
                else:
                    self._paint_open(surface, rect, noise, x + y)

    def _paint_open(self, surface, rect, noise: int, checker: int) -> None:
        surface.fill(C_OPEN if checker % 2 == 0 else C_OPEN_ALT, rect)
        # Roughly a third of open tiles get a scrap of vegetation.
        if noise % 3 == 0:
            tuft = C_TUFT if noise % 6 else C_TUFT_DRY
            ox = 2 + (noise >> 3) % (TILE - 6)
            oy = 3 + (noise >> 7) % (TILE - 7)
            surface.fill(tuft, (rect.x + ox, rect.y + oy + 1, 3, 1))
            surface.fill(tuft, (rect.x + ox + 1, rect.y + oy, 1, 2))
        if noise % 11 == 0:
            surface.fill(C_ROCK_FACE, (rect.x + 4 + noise % 4,
                                       rect.y + 6 + (noise >> 5) % 4, 2, 2))

    def _paint_rock(self, surface, rect, noise: int) -> None:
        """Broken stone, not paving.

        An identical lit band across the top of every tile lines up into
        continuous horizontal stripes and the whole ridge reads as a road.
        Irregular blocks placed from the tile hash read as rock instead.
        """
        surface.fill(C_ROCK, rect)
        blocks = (
            (0, 0, 6 + noise % 4, 4 + (noise >> 2) % 3),
            (5 + noise % 3, 2 + (noise >> 3) % 2, 6, 5),
            (1 + (noise >> 5) % 3, 7 + (noise >> 6) % 2, 7, 5),
        )
        for index, (bx, by, bw, bh) in enumerate(blocks):
            tone = C_ROCK_TOP if (noise >> index) & 1 else C_ROCK_FACE
            surface.fill(tone, pygame.Rect(rect.x + bx, rect.y + by, bw, bh)
                         .clip(rect))
            # A one-pixel lip on top of each block gives the stone some relief.
            surface.fill(shade(tone, 1.25),
                         pygame.Rect(rect.x + bx, rect.y + by, bw, 1).clip(rect))
            surface.fill(shade(C_ROCK, 0.7),
                         pygame.Rect(rect.x + bx, rect.y + by + bh - 1, bw, 1)
                         .clip(rect))

    def _paint_water(self, surface, rect, noise: int) -> None:
        surface.fill(C_WATER, rect)
        for index, base in enumerate((3, 8)):
            offset = (noise >> (index * 3)) % 5
            surface.fill(C_WATER_TOP,
                         (rect.x + offset, rect.y + base + index, 5, 1))
            surface.fill(shade(C_WATER_TOP, 0.8),
                         (rect.x + (offset + 7) % TILE, rect.y + base + 2, 3, 1))

    def _paint_forest(self, surface, rect, noise: int) -> None:
        surface.fill(C_FOREST, rect)
        # Two or three little conifers rather than a grid of identical blobs.
        spots = ((2, 3), (7, 2), (4, 8), (9, 7))
        for index, (cx, cy) in enumerate(spots):
            if (noise >> index) & 1 and index == 3:
                continue
            jitter = (noise >> (index * 2)) % 2
            tx, ty = rect.x + cx + jitter, rect.y + cy
            surface.fill(C_TRUNK, (tx + 1, ty + 3, 1, 2))
            surface.fill(C_FOREST_TOP, (tx, ty + 1, 3, 2))
            surface.fill(shade(C_FOREST_TOP, 1.2), (tx + 1, ty, 1, 2))

    # -- fog ---------------------------------------------------------------
    def set_fog(self, visible: set, explored: set | None = None) -> None:
        """Rebuild the shroud over the viewport, and only when it has moved.

        The shroud used to cover the whole board, which on a world meant
        iterating forty thousand tiles to shade the seven hundred anyone could
        see. It is now the size of the window, and the camera position is part
        of what makes it stale.
        """
        board = self.board
        explored = explored if explored is not None else visible
        key = (len(visible), len(explored), board.cam_x, board.cam_y)
        if self._fog_key == key:
            return
        self._fog_key = key
        self.fog.fill((0, 0, 0, 0))
        # Tile range the window covers, padded by one so a part-tile at the
        # edge is still shaded.
        left = max(0, (board.cam_x - board.pad_x) // TILE)
        top = max(0, (board.cam_y - board.pad_y) // TILE)
        for y in range(top, min(board.map.height, top + BOARD_H // TILE + 2)):
            for x in range(left, min(board.map.width, left + BOARD_W // TILE + 2)):
                tile = (x, y)
                if tile in visible:
                    continue
                alpha = FOG_SEEN_ALPHA if tile in explored else FOG_UNKNOWN_ALPHA
                self.fog.fill((6, 8, 14, alpha),
                              (board.ox + x * TILE - Board.VIEW.x,
                               board.oy + y * TILE - Board.VIEW.y, TILE, TILE))

    # -- world -------------------------------------------------------------
    # -- minimap -----------------------------------------------------------
    def minimap_base(self) -> pygame.Surface:
        """A tiny picture of the terrain, built once and kept.

        One pixel a tile at the smallest scale, so the whole thing is a few
        thousand pixels: cheap to build, free to blit, and rebuilt only when
        the match changes map.
        """
        if self._minimap is None:
            tilemap = self.board.map
            surface = pygame.Surface((tilemap.width, tilemap.height)).convert()
            for y in range(tilemap.height):
                for x in range(tilemap.width):
                    char = tilemap.at(x, y)
                    surface.set_at((x, y), MINIMAP_TERRAIN.get(char, C_OPEN))
            self._minimap = surface
        return self._minimap

    def _minimap_shaded(self, rect, explored) -> pygame.Surface:
        """The minimap's terrain with the unexplored blacked out, cached.

        Rebuilt only when you have scouted more ground, and built by painting
        the ground you *have* seen onto a dark surface rather than by blacking
        out the ground you have not -- so it costs what you have explored
        rather than the size of the world.

        Doing it the other way round, every frame, cost 159ms a frame on a
        98,000-tile continent: ten frames a second, all of it spent shading
        ground nobody has ever been to.
        """
        key = (rect.size, len(explored), id(self._minimap))
        if self._shade_key == key:
            return self._shaded
        base = self.minimap_base()
        small = pygame.Surface(base.get_size()).convert()
        small.fill((8, 10, 16))
        width, height = base.get_size()
        for (x, y) in explored:
            if 0 <= x < width and 0 <= y < height:
                small.set_at((x, y), base.get_at((x, y)))
        self._shaded = pygame.transform.scale(small, rect.size)
        self._shade_key = key
        return self._shaded

    def draw_minimap(self, dest: pygame.Surface, rect: pygame.Rect,
                     view, colors: dict, my_pid: int) -> None:
        """The whole world at a glance: ground, everyone on it, and the camera.

        Deliberately not a second battlefield -- a unit is one dot and there is
        no detail to read. Its job is "where is my army, where is the fighting,
        and what am I looking at", which is exactly what a scrolling map takes
        away.
        """
        board = self.board
        tilemap = board.map
        sx = rect.width / tilemap.width
        sy = rect.height / tilemap.height

        def at(tx, ty):
            return (rect.x + int(tx * sx), rect.y + int(ty * sy))

        pygame.draw.rect(dest, UI_PANEL_LO, rect.inflate(4, 4))
        pygame.draw.rect(dest, UI_PANEL_HI, rect.inflate(4, 4), 1)
        dest.blit(self._minimap_shaded(rect, view.explored), rect.topleft)

        step_x, step_y = max(1, int(sx) + 1), max(1, int(sy) + 1)
        for tile in tilemap.nodes:
            if tile not in view.explored:
                continue
            owner = view.node_owner.get(tile)
            colour = C_NODE if owner is None else team_color(colors.get(owner, 0))
            dest.fill(colour, (*at(*tile), step_x, step_y))

        blob = max(2, step_x)
        for building in view.buildings.values():
            x, y = at(building["x"], building["y"])
            dest.fill(team_color(colors.get(building["owner"], 0)),
                      (x - 1, y - 1, blob + 2, blob + 2))
        for unit in view.units.values():
            dest.fill(team_color(colors.get(unit["owner"], 0)),
                      (*at(unit["x"], unit["y"]), blob, blob))

        if board.scrolls:
            window = board.camera_tiles()
            corner = at(window.x, window.y)
            frame = pygame.Rect(corner[0], corner[1],
                                max(3, int(window.width * sx)),
                                max(3, int(window.height * sy)))
            pygame.draw.rect(dest, UI_ACCENT, frame.clip(rect), 1)

    def draw_terrain(self, dest: pygame.Surface) -> None:
        dest.fill(UI_BG)
        board = self.board
        span = PATCH * TILE
        first_x = max(0, board.cam_x // span)
        first_y = max(0, board.cam_y // span)
        last_x = (board.cam_x + BOARD_W) // span
        last_y = (board.cam_y + BOARD_H) // span
        wide = -(-board.map.width // PATCH)
        tall = -(-board.map.height // PATCH)
        for py in range(first_y, min(tall - 1, last_y) + 1):
            for px in range(first_x, min(wide - 1, last_x) + 1):
                dest.blit(self._patch(px, py),
                          (board.ox + px * span, board.oy + py * span))

    def draw_fog(self, dest: pygame.Surface) -> None:
        dest.blit(self.fog, Board.VIEW.topleft)

    def draw_nodes(self, dest: pygame.Surface, node_owner: dict,
                   colors: dict) -> None:
        """Supply crates on each node, painted in its holder's colours."""
        for tile in self.board.map.nodes:
            if not self.board.on_screen(tile):
                continue
            owner = node_owner.get(tile)
            colour = C_NODE if owner is None else team_color(colors.get(owner, 0))
            sprite = self.sprites.node(colour)
            x, y = self.board.to_screen(tile)
            dest.blit(sprite, (x + (TILE - sprite.get_width()) // 2,
                               y + (TILE - sprite.get_height()) // 2))

    def draw_building(self, dest: pygame.Surface, building: dict,
                      colour) -> None:
        under = building.get("under", 0)
        sprite = self.sprites.building(building["code"], colour, bool(under))
        x, y = self.board.to_screen((building["x"], building["y"]))
        dest.blit(_shadow(width=12), (x + 1, y + TILE - 3))
        dest.blit(sprite, (x, y))
        rect = self.board.rect((building["x"], building["y"]))
        if under:
            # Scaffolding, and the number of turns left, over a dimmed shell.
            stalled = building.get("stalled")
            for line in range(2, TILE, 4):
                dest.fill(shade(colour, 0.9), (x + 1, y + line, TILE - 2, 1))
            ui.draw_text(dest, str(under), rect.centerx, rect.centery - 4, 13,
                         UI_WARN if stalled else UI_ACCENT, anchor="center",
                         shadow=True)
            if stalled:
                # Nobody is working here. Say so loudly: the supply is already
                # spent and the tile is blocked until somebody finishes it.
                pygame.draw.rect(dest, UI_WARN, rect, 1)
                ui.draw_text(dest, "!", rect.right - 3, rect.y - 1, 15, UI_WARN,
                             anchor="topright", shadow=True)
        else:
            self._hp_pip(dest, rect, building["hp"],
                         BUILDING[building["code"]].hp)

    def draw_unit(self, dest: pygame.Surface, unit: dict, colour,
                  selected: bool = False, pos=None, facing_left: bool = False,
                  ghost: bool = False) -> None:
        code = unit["code"]
        cx, cy = pos if pos else self.board.centre((unit["x"], unit["y"]))
        # A soft shadow under the feet. Without it a twelve-pixel sprite sinks
        # into a mid-green field and the board reads as mush; with it every
        # unit sits clearly on top of the terrain.
        if not ghost:
            dest.blit(_shadow(), (cx - 5, cy + 2))
        sprite = self.sprites.unit(code, colour, facing_left, ghost)
        dest.blit(sprite, (cx - sprite.get_width() // 2,
                           cy - sprite.get_height() // 2))
        if selected:
            pygame.draw.rect(dest, UI_ACCENT,
                             pygame.Rect(cx - 7, cy - 7, 15, 15), 1)
        if not ghost:
            self._hp_pip(dest, pygame.Rect(cx - 6, cy - 8, 13, 12),
                         unit.get("hp", 1), unit.get("max") or UNIT[code].hp)
            self._rank_pips(dest, cx, cy, unit.get("rank", 0))

    def _rank_pips(self, dest, cx: int, cy: int, rank: int) -> None:
        """Chevrons down the unit's left side, one per rank.

        Down the side rather than above the head, where the health hairline
        already lives -- and gold, because a promoted unit is the one thing on
        the board worth picking out of a crowd at a glance.
        """
        for index in range(min(rank, 3)):
            dest.fill((22, 18, 10), (cx - 8, cy - 4 + index * 3, 3, 2))
            dest.fill(UI_ACCENT, (cx - 8, cy - 4 + index * 3, 2, 1))

    def _hp_pip(self, dest, rect, hp: int, full: int) -> None:
        """A hairline of health, only when hurt.

        Kept to nine pixels by one: the earlier version was a 13x4 black slab
        hovering over a 12-pixel sprite, which drew more attention than the
        unit it belonged to.
        """
        if hp >= full:
            return
        fraction = max(0.0, min(1.0, hp / max(1, full)))
        bar = pygame.Rect(rect.centerx - 4, rect.top - 1, 9, 1)
        # Dark red behind rather than black: a nearly empty bar then still
        # reads as "badly hurt" instead of as a black box stuck to the sprite.
        dest.fill((16, 12, 14), bar.inflate(2, 2))
        dest.fill((92, 30, 32), bar)
        dest.fill(health_color(fraction),
                  (bar.x, bar.y, max(1, int(bar.width * fraction)), bar.height))

    def draw_order_path(self, dest: pygame.Surface, start, tiles, colour,
                        attack: bool) -> None:
        """A dotted line showing where a unit has been told to go."""
        points = [self.board.centre(start)] + [self.board.centre(t) for t in tiles]
        tint = UI_WARN if attack else colour
        for index in range(len(points) - 1):
            ax, ay = points[index]
            bx, by = points[index + 1]
            steps = max(abs(bx - ax), abs(by - ay)) // 3 or 1
            for step in range(steps):
                px = ax + (bx - ax) * step // steps
                py = ay + (by - ay) * step // steps
                dest.fill(tint, (px, py, 2, 2))
        if points:
            end = points[-1]
            marker = [(end[0], end[1] - 4), (end[0] + 4, end[1]),
                      (end[0], end[1] + 4), (end[0] - 4, end[1])]
            pygame.draw.polygon(dest, tint, marker, 1)

    def draw_selection_box(self, dest: pygame.Surface, anchor, current) -> None:
        rect = pygame.Rect(min(anchor[0], current[0]), min(anchor[1], current[1]),
                           abs(current[0] - anchor[0]), abs(current[1] - anchor[1]))
        pygame.draw.rect(dest, UI_ACCENT, rect, 1)

    def highlight(self, dest: pygame.Surface, tile, colour) -> None:
        pygame.draw.rect(dest, colour, self.board.rect(tile), 1)


TOOLTIP_MAX_W = 250


def draw_tooltip(dest: pygame.Surface, lines: list, near) -> None:
    """A small panel of (text, colour, size) rows, kept on screen.

    Flips to the other side of the cursor rather than being clipped, because a
    tooltip that runs off the edge is worse than none.
    """
    if not lines:
        return
    rows = [(str(text), colour, size) for text, colour, size in lines]
    height = 6 + sum(size - 3 for _text, _c, size in rows)
    # Size to the content rather than to a guess: a fixed width either clips
    # the longest blurb or leaves a wide empty box beside a two-word label.
    width = min(TOOLTIP_MAX_W,
                max(ui.text(text, size).get_width() for text, _c, size in rows) + 12)
    x = near[0] + 12
    y = near[1] + 10
    if x + width > SCREEN_W - 2:
        x = near[0] - width - 8
    if y + height > SCREEN_H - 2:
        y = near[1] - height - 8
    x = max(2, x)
    y = max(2, y)
    rect = pygame.Rect(x, y, width, height)
    panel = pygame.Surface(rect.size, pygame.SRCALPHA)
    panel.fill((14, 16, 24, 240))
    dest.blit(panel, rect.topleft)
    pygame.draw.rect(dest, UI_PANEL_HI, rect, 1)
    cy = rect.y + 3
    for text, colour, size in rows:
        ui.draw_text(dest, text, rect.x + 6, cy, size, colour)
        cy += size - 3


_SHADOWS: dict = {}


def _shadow(width: int = 10, height: int = 3) -> pygame.Surface:
    """A translucent smudge, cached by size."""
    got = _SHADOWS.get((width, height))
    if got is None:
        got = pygame.Surface((width, height), pygame.SRCALPHA)
        pygame.draw.ellipse(got, (0, 0, 0, 90), got.get_rect())
        pygame.draw.ellipse(got, (0, 0, 0, 55), got.get_rect().inflate(0, 2))
        _SHADOWS[(width, height)] = got
    return got


def _tile_hash(x: int, y: int) -> int:
    """A stable pseudo-random value per tile, so detail never shimmers."""
    value = (x * 73856093) ^ (y * 19349663)
    value ^= (value >> 13)
    return (value * 83492791) & 0x7FFFFFFF


__all__ = ["Board", "Renderer", "TILE", "TOP_H", "HUD_H", "PANEL_W",
           "BOARD_W", "BOARD_H", "SCREEN_W", "SCREEN_H",
           "UI_PANEL", "UI_PANEL_HI", "UI_PANEL_LO", "UI_TEXT", "UI_DIM",
           "UI_ACCENT", "UI_WARN", "UI_BG"]
