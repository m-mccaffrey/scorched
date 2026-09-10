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

from ..grid import FOREST, NODE, ROCK, WATER
from ..units import BUILDING, UNIT

TILE = 14
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
C_GRID = (0, 0, 0, 40)

#: Explored-but-unseen ground is dimmed; never-explored ground is blacked out.
FOG_SEEN_ALPHA = 130
FOG_UNKNOWN_ALPHA = 246


class Board:
    """Maps tile coordinates to screen pixels and back."""

    def __init__(self, tilemap) -> None:
        self.map = tilemap
        self.pixel_w = tilemap.width * TILE
        self.pixel_h = tilemap.height * TILE
        # Centre small maps in the board area rather than jamming them into
        # the corner.
        self.ox = max(0, (BOARD_W - self.pixel_w) // 2)
        self.oy = TOP_H + max(0, (BOARD_H - self.pixel_h) // 2)

    def to_screen(self, tile) -> tuple[int, int]:
        return (self.ox + tile[0] * TILE, self.oy + tile[1] * TILE)

    def centre(self, tile) -> tuple[int, int]:
        x, y = self.to_screen(tile)
        return (x + TILE // 2, y + TILE // 2)

    def to_tile(self, pos) -> tuple[int, int] | None:
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
        self.terrain = pygame.Surface((1, 1))
        self.fog = pygame.Surface((1, 1), pygame.SRCALPHA)
        self._fog_key = None

    # -- setup -------------------------------------------------------------
    def begin_match(self, tilemap) -> None:
        self.board = Board(tilemap)
        self.terrain = pygame.Surface((self.board.pixel_w,
                                       self.board.pixel_h)).convert()
        self._paint_terrain()
        self.fog = pygame.Surface((self.board.pixel_w, self.board.pixel_h),
                                  pygame.SRCALPHA)
        self._fog_key = None

    def _paint_terrain(self) -> None:
        tilemap = self.board.map
        surface = self.terrain
        for y in range(tilemap.height):
            for x in range(tilemap.width):
                char = tilemap.at(x, y)
                rect = pygame.Rect(x * TILE, y * TILE, TILE, TILE)
                if char == ROCK:
                    surface.fill(C_ROCK, rect)
                    surface.fill(C_ROCK_TOP, (rect.x, rect.y, TILE, 3))
                elif char == WATER:
                    surface.fill(C_WATER, rect)
                    surface.fill(C_WATER_TOP, (rect.x, rect.y + 4, TILE, 1))
                    surface.fill(C_WATER_TOP, (rect.x, rect.y + 9, TILE, 1))
                elif char == FOREST:
                    surface.fill(C_FOREST, rect)
                    for cx, cy in ((3, 4), (8, 3), (5, 9), (10, 8)):
                        surface.fill(C_FOREST_TOP,
                                     (rect.x + cx, rect.y + cy, 3, 3))
                else:
                    # A quiet checker so the grid is readable without lines.
                    surface.fill(C_OPEN if (x + y) % 2 == 0 else C_OPEN_ALT, rect)
                    if char == NODE:
                        pygame.draw.rect(surface, shade(C_NODE, 0.5),
                                         rect.inflate(-2, -2), 1)

    # -- fog ---------------------------------------------------------------
    def set_fog(self, visible: set, explored: set | None = None) -> None:
        """Rebuild the shroud, skipping the work when nothing has changed."""
        explored = explored if explored is not None else visible
        key = (len(visible), len(explored))
        if self._fog_key == key:
            return
        self._fog_key = key
        self.fog.fill((0, 0, 0, 0))
        tilemap = self.board.map
        for y in range(tilemap.height):
            for x in range(tilemap.width):
                tile = (x, y)
                if tile in visible:
                    continue
                alpha = FOG_SEEN_ALPHA if tile in explored else FOG_UNKNOWN_ALPHA
                self.fog.fill((6, 8, 14, alpha), (x * TILE, y * TILE, TILE, TILE))

    # -- world -------------------------------------------------------------
    def draw_terrain(self, dest: pygame.Surface) -> None:
        dest.fill(UI_BG)
        dest.blit(self.terrain, (self.board.ox, self.board.oy))

    def draw_fog(self, dest: pygame.Surface) -> None:
        dest.blit(self.fog, (self.board.ox, self.board.oy))

    def draw_nodes(self, dest: pygame.Surface, node_owner: dict,
                   colors: dict) -> None:
        """A diamond on each resource node, tinted by who holds it."""
        for tile in self.board.map.nodes:
            owner = node_owner.get(tile)
            colour = C_NODE if owner is None else team_color(colors.get(owner, 0))
            cx, cy = self.board.centre(tile)
            points = [(cx, cy - 5), (cx + 5, cy), (cx, cy + 5), (cx - 5, cy)]
            pygame.draw.polygon(dest, colour, points)
            pygame.draw.polygon(dest, shade(colour, 0.45), points, 1)

    def draw_building(self, dest: pygame.Surface, building: dict,
                      colour) -> None:
        rect = self.board.rect((building["x"], building["y"])).inflate(-1, -1)
        under = building.get("under", 0)
        body = shade(colour, 0.45) if under else shade(colour, 0.8)
        dest.fill(body, rect)
        pygame.draw.rect(dest, shade(colour, 1.3) if not under else UI_DIM,
                         rect, 1)
        if building["code"] == "base":
            inner = rect.inflate(-6, -6)
            dest.fill(shade(colour, 1.25), inner)
        else:
            dest.fill(shade(colour, 1.15), (rect.x + 3, rect.centery - 1,
                                            rect.width - 6, 3))
        if under:
            ui.draw_text(dest, str(under), rect.centerx, rect.centery, 13,
                         UI_ACCENT, anchor="center")
        else:
            self._hp_pip(dest, rect, building["hp"],
                         BUILDING[building["code"]].hp)

    def draw_unit(self, dest: pygame.Surface, unit: dict, colour,
                  selected: bool = False, pos=None) -> None:
        code = unit["code"]
        cx, cy = pos if pos else self.board.centre((unit["x"], unit["y"]))
        light = shade(colour, 1.35)
        dark = shade(colour, 0.45)

        if code == "scout":
            pygame.draw.circle(dest, colour, (cx, cy), 4)
            pygame.draw.circle(dest, light, (cx, cy - 1), 2)
        elif code == "trooper":
            rect = pygame.Rect(cx - 4, cy - 4, 9, 9)
            dest.fill(colour, rect)
            dest.fill(light, (rect.x, rect.y, rect.width, 2))
            pygame.draw.rect(dest, dark, rect, 1)
        elif code == "ranged":
            points = [(cx, cy - 5), (cx + 5, cy + 4), (cx - 5, cy + 4)]
            pygame.draw.polygon(dest, colour, points)
            pygame.draw.polygon(dest, dark, points, 1)
        else:  # bruiser
            rect = pygame.Rect(cx - 5, cy - 5, 11, 11)
            dest.fill(dark, rect)
            dest.fill(colour, rect.inflate(-3, -3))
            dest.fill(light, (rect.x + 2, rect.y + 2, rect.width - 4, 2))

        if selected:
            pygame.draw.rect(dest, UI_ACCENT,
                             pygame.Rect(cx - 7, cy - 7, 15, 15), 1)
        self._hp_pip(dest, pygame.Rect(cx - 6, cy - 8, 13, 12),
                     unit.get("hp", 1), UNIT[code].hp)

    def _hp_pip(self, dest, rect, hp: int, full: int) -> None:
        if hp >= full:
            return
        fraction = max(0.0, min(1.0, hp / max(1, full)))
        bar = pygame.Rect(rect.centerx - 5, rect.top - 3, 11, 2)
        dest.fill((14, 14, 18), bar.inflate(2, 2))
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


__all__ = ["Board", "Renderer", "TILE", "TOP_H", "HUD_H", "PANEL_W",
           "BOARD_W", "BOARD_H", "SCREEN_W", "SCREEN_H",
           "UI_PANEL", "UI_PANEL_HI", "UI_PANEL_LO", "UI_TEXT", "UI_DIM",
           "UI_ACCENT", "UI_WARN", "UI_BG"]
