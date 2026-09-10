"""World rendering.

Two ideas keep this fast enough for a Pi 400 at 60 fps:

1. The sky and the dirt are composited once into a single ``scene`` surface.
   Per frame the world costs exactly one opaque blit, not 640 column draws.
2. When a shell moves dirt, only the columns it touched are repainted -- sky
   region back in, new dirt on top.  A Nuke dirties 130 columns out of 640.

Everything else on screen (tanks, shells, fire) is a few dozen small draws.
"""

from __future__ import annotations

import math
import random

import pygame

from ..terrain import HUD_H, PLAY_H, WORLD_W
from .palette import (GROUNDS, SHIELD, SKIES, UI_ACCENT, UI_DIM, UI_PANEL,
                      UI_PANEL_LO, UI_TEXT, UI_WARN, health_color, pick_scheme,
                      shade)
from lanlib import ui

UI_GOOD_CASH = (150, 220, 150)

TANK_W = 13
TANK_H = 9
BARREL_LEN = 10


class Renderer:
    """Owns every cached surface the game view needs."""

    def __init__(self) -> None:
        self.sky = pygame.Surface((WORLD_W, PLAY_H)).convert()
        self.scene = pygame.Surface((WORLD_W, PLAY_H)).convert()
        self.ground = GROUNDS[0]
        self.sky_scheme = SKIES[0]
        self.terrain = None
        self._tanks: dict[int, pygame.Surface] = {}
        self._dead: dict[int, pygame.Surface] = {}
        self._strata: list[int] = []

    # -- round setup -----------------------------------------------------
    def begin_round(self, terrain, seed: int, colors) -> None:
        self.terrain = terrain
        self.sky_scheme, self.ground = pick_scheme(seed)
        self._build_sky(seed)
        self._build_strata(seed)
        self._build_tank_sprites(colors)
        self.repaint_all()

    def _build_sky(self, seed: int) -> None:
        top, horizon, stars = self.sky_scheme
        rng = random.Random(seed ^ 0x5EED)
        sky = self.sky
        # Ordered dithering between the two band colours: at this resolution it
        # reads as texture rather than as banding, which is exactly the look.
        for y in range(PLAY_H):
            t = y / max(1, PLAY_H - 1)
            t = t ** 1.35
            base = tuple(int(top[i] + (horizon[i] - top[i]) * t) for i in range(3))
            sky.fill(base, (0, y, WORLD_W, 1))
        if stars:
            for _ in range(90):
                x = rng.randrange(WORLD_W)
                y = rng.randrange(int(PLAY_H * 0.55))
                bright = rng.choice((160, 200, 235, 255))
                sky.set_at((x, y), (bright, bright, max(180, bright)))
        self._draw_celestial(rng, stars)

    def _draw_celestial(self, rng: random.Random, night: bool) -> None:
        """A sun or moon low in the sky -- one shape, a lot of atmosphere."""
        cx = rng.randrange(60, WORLD_W - 60)
        cy = rng.randrange(40, int(PLAY_H * 0.42))
        radius = rng.randint(12, 20)
        core = (226, 232, 240) if night else (255, 236, 176)
        halo = shade(core, 0.42)
        for r in range(radius + 7, radius, -1):
            t = (r - radius) / 7.0
            blend = tuple(int(halo[i] * (1 - t) + self.sky.get_at((cx, cy))[i] * t)
                          for i in range(3))
            pygame.draw.circle(self.sky, blend, (cx, cy), r)
        pygame.draw.circle(self.sky, core, (cx, cy), radius)
        if night:
            # Bite a crescent out of the moon with the sky colour behind it.
            behind = self.sky.get_at((max(0, cx - radius * 2), cy))
            pygame.draw.circle(self.sky, behind[:3],
                               (cx - radius // 2, cy - radius // 3), radius)

    def _build_strata(self, seed: int) -> None:
        """Horizontal rock layers, so deep craters expose something."""
        rng = random.Random(seed ^ 0xB00C)
        y = rng.randint(40, 70)
        self._strata = []
        while y < PLAY_H:
            self._strata.append(y)
            y += rng.randint(34, 62)

    def _build_tank_sprites(self, colors) -> None:
        self._tanks.clear()
        self._dead.clear()
        for index, color in enumerate(colors):
            self._tanks[index] = _tank_sprite(color)
            self._dead[index] = _tank_sprite(shade(color, 0.28), wrecked=True)

    # -- terrain compositing ----------------------------------------------
    def repaint_all(self) -> None:
        self.scene.blit(self.sky, (0, 0))
        self._paint_columns(0, WORLD_W)
        if self.terrain is not None:
            self.terrain.take_dirty()

    def refresh_terrain(self) -> None:
        """Repaint only what the last explosion changed."""
        if self.terrain is None:
            return
        lo, hi = self.terrain.take_dirty()
        if hi <= lo:
            return
        lo = max(0, lo - 1)
        hi = min(WORLD_W, hi + 1)
        region = pygame.Rect(lo, 0, hi - lo, PLAY_H)
        self.scene.blit(self.sky, region.topleft, region)
        self._paint_columns(lo, hi)

    def _paint_columns(self, lo: int, hi: int) -> None:
        terrain = self.terrain
        if terrain is None:
            return
        crust, body, deep = self.ground
        crust_hi = shade(crust, 1.22)
        scene = self.scene
        heights = terrain.height
        strata = self._strata
        stratum_color = shade(body, 0.90)
        for x in range(lo, hi):
            top = heights[x]
            if top >= PLAY_H:
                continue
            # Body, then the darker deep zone, then rock layers, then the
            # bright crust on top. Four fills per column, all opaque.
            scene.fill(body, (x, top, 1, PLAY_H - top))
            deep_start = min(PLAY_H, top + 90)
            if deep_start < PLAY_H:
                scene.fill(deep, (x, deep_start, 1, PLAY_H - deep_start))
            for line_y in strata:
                if line_y > top + 20:
                    scene.fill(stratum_color, (x, line_y, 1, 1))
            scene.fill(crust, (x, top, 1, min(4, PLAY_H - top)))
            scene.fill(crust_hi, (x, top, 1, 1))

    # -- world ------------------------------------------------------------
    def draw_world(self, dest: pygame.Surface) -> None:
        dest.blit(self.scene, (0, 0))

    def draw_tank(self, dest: pygame.Surface, player: dict, aiming: bool,
                  highlight: bool = False) -> None:
        color_index = player["color"]
        x, y = int(player["x"]), int(player["y"])
        alive = player["alive"]
        sprite = (self._tanks if alive else self._dead).get(color_index)
        if sprite is None:
            return
        dest.blit(sprite, (x - TANK_W // 2, y - TANK_H + 1))
        if not alive:
            return

        color = _team_color(color_index)
        if player.get("shield", 0) > 0:
            self._draw_shield(dest, x, y, player)

        # Barrel
        rad = math.radians(player["angle"])
        bx, by = x, y - 6
        ex = bx + math.cos(rad) * BARREL_LEN
        ey = by - math.sin(rad) * BARREL_LEN
        pygame.draw.line(dest, (16, 16, 20), (bx, by + 1), (ex, ey + 1), 2)
        pygame.draw.line(dest, shade(color, 1.3), (bx, by), (ex, ey), 2)

        if highlight:
            # A blinking chevron over whoever is up, so nobody has to hunt for
            # their own tank on a busy map.
            bob = int(math.sin(pygame.time.get_ticks() * 0.006) * 2)
            top = y - TANK_H - 8 + bob
            pygame.draw.polygon(dest, UI_ACCENT,
                                [(x, top + 5), (x - 4, top), (x + 4, top)])

    def _draw_shield(self, dest: pygame.Surface, x: int, y: int, player: dict) -> None:
        strength = player["shield"] / max(1, player.get("shield_max", 1))
        radius = 15
        alpha = int(70 + 110 * strength)
        bubble = pygame.Surface((radius * 2 + 2, radius * 2 + 2), pygame.SRCALPHA)
        pygame.draw.circle(bubble, (*SHIELD, alpha // 3), (radius + 1, radius + 1), radius)
        pygame.draw.circle(bubble, (*SHIELD, alpha), (radius + 1, radius + 1), radius, 1)
        dest.blit(bubble, (x - radius - 1, y - 5 - radius - 1))

    def draw_name_tags(self, dest: pygame.Surface, players: list, turn_pid: int,
                       me: int) -> None:
        for player in players:
            if not player["alive"]:
                continue
            x, y = int(player["x"]), int(player["y"])
            color = _team_color(player["color"])
            label = player["name"]
            if player["pid"] == me:
                label = f"{label} (you)"
            surf = ui.text(label, 14, color)
            tag_x = max(1, min(WORLD_W - surf.get_width() - 1,
                               x - surf.get_width() // 2))
            tag_y = y - TANK_H - 20
            dest.blit(ui.text(label, 14, (10, 10, 14)), (tag_x + 1, tag_y + 1))
            dest.blit(surf, (tag_x, tag_y))
            # Health pip under the name
            bar = pygame.Rect(x - 10, tag_y + 11, 20, 3)
            frac = player["hp"] / max(1, player["max_hp"])
            dest.fill((12, 12, 16), bar.inflate(2, 2))
            if frac > 0:
                dest.fill(health_color(frac),
                          (bar.x, bar.y, max(1, int(bar.width * frac)), bar.height))


def _team_color(index: int):
    from ..game import TEAM_COLORS
    return TEAM_COLORS[index % len(TEAM_COLORS)]


def _tank_sprite(color, wrecked: bool = False) -> pygame.Surface:
    """Build one 13x9 tank. Small enough to read as a shape, not a picture."""
    surf = pygame.Surface((TANK_W, TANK_H), pygame.SRCALPHA)
    tread = (44, 44, 52) if not wrecked else (34, 32, 34)
    hull = color
    hull_hi = shade(color, 1.35)
    hull_lo = shade(color, 0.6)

    # Treads with visible road wheels.
    pygame.draw.rect(surf, tread, (0, 6, TANK_W, 3))
    for wx in range(1, TANK_W - 1, 3):
        surf.set_at((wx, 7), shade(tread, 1.7))

    if wrecked:
        # A burnt-out hulk: broken silhouette, no turret.
        pygame.draw.rect(surf, hull, (2, 4, 6, 2))
        pygame.draw.rect(surf, hull_lo, (8, 5, 3, 1))
        return surf

    pygame.draw.rect(surf, hull, (1, 3, TANK_W - 2, 3))
    pygame.draw.rect(surf, hull_hi, (1, 3, TANK_W - 2, 1))
    pygame.draw.rect(surf, hull_lo, (1, 5, TANK_W - 2, 1))
    pygame.draw.rect(surf, hull, (4, 1, 5, 2))
    pygame.draw.rect(surf, hull_hi, (4, 1, 5, 1))
    return surf


# ---------------------------------------------------------------------------
# HUD
# ---------------------------------------------------------------------------

class Hud:
    """The status bar and the top strip."""

    def __init__(self) -> None:
        self.message = ""
        self.message_until = 0.0

    def flash(self, text_msg: str, seconds: float = 2.5) -> None:
        self.message = text_msg
        self.message_until = pygame.time.get_ticks() / 1000.0 + seconds

    # -- top strip --------------------------------------------------------
    def draw_top(self, dest: pygame.Surface, state) -> None:
        overlay = pygame.Surface((WORLD_W, 16), pygame.SRCALPHA)
        overlay.fill((10, 10, 16, 120))
        dest.blit(overlay, (0, 0))

        ui.draw_text(dest, f"ROUND {state.round}/{state.rounds}", 6, 3, 15, UI_TEXT)
        self._draw_wind(dest, state.wind, WORLD_W // 2, 8)

        if state.time_left >= 0 and state.phase == "aim":
            urgent = state.time_left <= 5
            color = UI_WARN if urgent else UI_DIM
            ui.draw_text(dest, f"{int(state.time_left):>3}s", WORLD_W - 6, 3, 15,
                         color, anchor="topright")

    def _draw_wind(self, dest: pygame.Surface, wind: int, cx: int, cy: int) -> None:
        ui.draw_text(dest, "WIND", cx - 44, cy, 14, UI_DIM, anchor="midright")
        span = 52
        bar = pygame.Rect(cx - span // 2, cy - 3, span, 6)
        dest.fill((22, 22, 30), bar)
        pygame.draw.rect(dest, UI_PANEL_LO, bar, 1)
        pygame.draw.line(dest, UI_DIM, (cx, bar.top), (cx, bar.bottom - 1))
        if wind:
            magnitude = min(1.0, abs(wind) / 100.0)
            width = int((span // 2 - 2) * magnitude)
            if width > 0:
                x = cx if wind > 0 else cx - width
                dest.fill(UI_ACCENT, (x, bar.top + 2, width, 2))
                tip = cx + width if wind > 0 else cx - width
                direction = 1 if wind > 0 else -1
                pygame.draw.polygon(dest, UI_ACCENT, [
                    (tip + 3 * direction, cy), (tip, cy - 3), (tip, cy + 3)])
        ui.draw_text(dest, f"{abs(wind)}", cx + 44, cy, 14, UI_DIM, anchor="midleft")

    # -- bottom bar --------------------------------------------------------
    def draw_bottom(self, dest: pygame.Surface, state) -> None:
        bar = pygame.Rect(0, PLAY_H, WORLD_W, HUD_H)
        ui.draw_panel(dest, bar, UI_PANEL)

        me = state.players.get(state.my_pid)
        active = state.players.get(state.turn_pid)
        subject = active if active else me
        if subject is None:
            return

        color = _team_color(subject["color"])
        y0 = PLAY_H + 4

        # -- who is up
        label = subject["name"]
        if subject["pid"] == state.my_pid:
            label += "  <- YOU"
        ui.draw_text(dest, label, 8, y0, 18, color)

        hp_frac = subject["hp"] / max(1, subject["max_hp"])
        ui.draw_bar(dest, (8, y0 + 16, 110, 7), hp_frac, health_color(hp_frac))
        ui.draw_text(dest, f"{subject['hp']}", 122, y0 + 15, 15, UI_DIM)
        if subject.get("shield", 0) > 0:
            ui.draw_bar(dest, (8, y0 + 26, 110, 4),
                        subject["shield"] / max(1, subject["shield_max"]), SHIELD)

        # -- aim readouts
        self._readout(dest, 160, y0, "ANGLE", f"{subject['angle']}", "°")
        self._readout(dest, 232, y0, "POWER", f"{subject['power']}", "")

        # -- weapon
        from ..weapons import WEAPON_BY_CODE
        weapon = WEAPON_BY_CODE.get(subject["weapon"])
        if weapon is not None:
            ui.draw_text(dest, "WEAPON", 310, y0, 14, UI_DIM)
            ui.draw_text(dest, weapon.name, 310, y0 + 12, 17, UI_ACCENT)
            ammo = state.my_ammo(weapon.code) if subject["pid"] == state.my_pid else None
            if ammo is not None:
                shown = "UNLIMITED" if ammo < 0 else f"x{ammo}"
                ui.draw_text(dest, shown, 310, y0 + 27, 15,
                             UI_DIM if ammo < 0 else UI_TEXT)

        # -- fuel and cash
        ui.draw_text(dest, "FUEL", 430, y0, 14, UI_DIM)
        ui.draw_bar(dest, (430, y0 + 13, 64, 6),
                    subject["fuel"] / 200.0, (140, 190, 255))
        ui.draw_text(dest, f"${subject['cash']:,}", 430, y0 + 24, 15, UI_GOOD_CASH)

        self._draw_roster(dest, state, 512, PLAY_H + 3)

        now = pygame.time.get_ticks() / 1000.0
        if self.message and now < self.message_until:
            ui.draw_text(dest, self.message, WORLD_W // 2, PLAY_H - 12, 17,
                         UI_ACCENT, anchor="center", shadow=True)

    def _readout(self, dest, x, y, title, value, suffix) -> None:
        ui.draw_text(dest, title, x, y, 14, UI_DIM)
        ui.draw_text(dest, value + suffix, x, y + 12, 22, UI_TEXT)

    def _draw_roster(self, dest, state, x: int, y: int) -> None:
        """Compact scoreboard: colour chip, name, health bar."""
        players = sorted(state.players.values(), key=lambda p: p["pid"])
        row_h = 10
        for i, player in enumerate(players[:4]):
            ry = y + i * row_h
            self._roster_row(dest, player, x, ry, state)
        for i, player in enumerate(players[4:8]):
            ry = y + i * row_h
            self._roster_row(dest, player, x + 66, ry, state)

    def _roster_row(self, dest, player, x, y, state) -> None:
        color = _team_color(player["color"])
        dead = not player["alive"]
        dest.fill(shade(color, 0.4) if dead else color, (x, y + 2, 5, 5))
        name = player["name"][:7]
        ui.draw_text(dest, name, x + 8, y, 13,
                     UI_DIM if dead else UI_TEXT)
        frac = 0.0 if dead else player["hp"] / max(1, player["max_hp"])
        ui.draw_bar(dest, (x + 8 + 40, y + 2, 18, 5), frac,
                    health_color(frac) if frac else (60, 60, 66))
        if player["pid"] == state.turn_pid:
            pygame.draw.polygon(dest, UI_ACCENT,
                                [(x - 5, y + 2), (x - 5, y + 8), (x - 1, y + 5)])

