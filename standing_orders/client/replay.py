"""Playing a turn back.

The server hands over a list of beat-stamped events. This walks them at a
watchable pace and turns them into movement, tracer fire and smoke. Because
the timeline is fixed before the first frame is drawn, a slow machine simply
finishes late -- it cannot desync, and the server's grace period covers it.
"""

from __future__ import annotations

import math
import random

import pygame

from lanlib import ui
from lanlib.theme import UI_ACCENT, UI_TEXT, UI_WARN, shade, team_color

from ..resolve import SUBTICKS
from .render import TILE

#: Seconds of wall clock per beat. Twelve beats plus a tail makes a turn about
#: four seconds -- long enough to read, short enough that nobody sighs.
BEAT_SECONDS = 0.30
TAIL_BEATS = 1.6


class Puff:
    __slots__ = ("x", "y", "vx", "vy", "age", "life", "colour", "size")

    def __init__(self, x, y, vx, vy, life, colour, size=2):
        self.x, self.y, self.vx, self.vy = x, y, vx, vy
        self.age, self.life, self.colour, self.size = 0.0, life, colour, size

    def update(self, dt) -> bool:
        self.age += dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.vy += 26 * dt
        return self.age < self.life


class Tracer:
    __slots__ = ("a", "b", "age", "life", "colour")

    def __init__(self, a, b, colour, life=0.16):
        self.a, self.b, self.colour, self.life = a, b, colour, life
        self.age = 0.0

    def update(self, dt) -> bool:
        self.age += dt
        return self.age < self.life


class Floater:
    __slots__ = ("x", "y", "text", "colour", "age", "life")

    def __init__(self, x, y, text, colour, life=1.1):
        self.x, self.y, self.text, self.colour = x, y, text, colour
        self.age, self.life = 0.0, life

    def update(self, dt) -> bool:
        self.age += dt
        self.y -= 14 * dt
        return self.age < self.life


class ReplayPlayer:
    """Animates one turn against the client's world view."""

    def __init__(self, payload: dict, view, renderer, colors: dict,
                 sfx=None) -> None:
        self.seq = payload.get("seq", 0)
        self.events = sorted(payload.get("events", []), key=lambda e: e["s"])
        self.final_state = payload.get("state")
        self.actors = {int(k): v for k, v in payload.get("actors", {}).items()}
        self.view = view
        self.renderer = renderer
        self.colors = colors
        self.sfx = sfx
        self.beat = 0.0
        self.index = 0
        self.finished = False
        self.total = SUBTICKS + TAIL_BEATS
        self._rng = random.Random(self.seq)
        self._puffs: list = []
        self._tracers: list = []
        self._floaters: list = []
        #: uid -> (from_tile, to_tile, beat the step began)
        self._motion: dict = {}
        self._revealed: set = set()

    # -- update ------------------------------------------------------------
    def update(self, dt: float) -> bool:
        self.beat += dt / BEAT_SECONDS
        while self.index < len(self.events) and self.events[self.index]["s"] <= self.beat:
            self._apply(self.events[self.index])
            self.index += 1
        self._puffs = [p for p in self._puffs if p.update(dt)]
        self._tracers = [t for t in self._tracers if t.update(dt)]
        self._floaters = [f for f in self._floaters if f.update(dt)]
        if self.beat >= self.total and self.index >= len(self.events):
            self.finished = True
        return not self.finished

    def skip(self) -> None:
        """Jump to the end -- for a quiet turn nobody wants to sit through."""
        while self.index < len(self.events):
            self._apply(self.events[self.index])
            self.index += 1
        self.beat = self.total
        self.finished = True

    def _ensure_unit(self, uid: int, tile=None) -> dict | None:
        """A unit can walk into view mid-turn; build it from the actor roster."""
        unit = self.view.units.get(uid)
        if unit is not None:
            return unit
        info = self.actors.get(uid)
        if info is None:
            return None
        unit = {"uid": uid, "owner": info["owner"], "code": info["code"],
                "hp": info.get("hp", 1), "x": tile[0] if tile else 0,
                "y": tile[1] if tile else 0}
        self.view.units[uid] = unit
        self.view.remembered.pop(uid, None)
        return unit

    def _apply(self, event: dict) -> None:
        kind = event["e"]
        beat = event["s"]

        if kind == "move":
            tile = tuple(event["to"])
            unit = self._ensure_unit(event["uid"], tile)
            if unit is None:
                return
            self._motion[unit["uid"]] = ((unit["x"], unit["y"]), tile, beat)
            unit["x"], unit["y"] = tile
            self._revealed.add(tile)

        elif kind == "shoot":
            shooter = self._ensure_unit(event["uid"], tuple(event["at"]))
            colour = team_color(self.colors.get(
                shooter["owner"] if shooter else 0, 0))
            self._tracers.append(Tracer(self._pos(tuple(event["at"])),
                                        self._pos(tuple(event["to"])), colour))
            self._revealed.update({tuple(event["at"]), tuple(event["to"])})
            if event.get("kind") == "unit":
                target = self._ensure_unit(event["tgt"], tuple(event["to"]))
                if target is not None:
                    target["hp"] = event["hp"]
            else:
                building = self.view.buildings.get(event["tgt"])
                if building is not None:
                    building["hp"] = event["hp"]
            if self.sfx:
                self.sfx.play("shot")

        elif kind == "kill":
            tile = tuple(event["at"])
            self._revealed.add(tile)
            if event.get("kind") == "building":
                self.view.buildings.pop(event["uid"], None)
                self._burst(tile, 26, (220, 150, 60), spread=70)
                self._floaters.append(Floater(*self._pos(tile), "DESTROYED",
                                              UI_WARN, life=1.6))
                if self.sfx:
                    self.sfx.play("boom")
            else:
                self.view.units.pop(event["uid"], None)
                self.view.remembered.pop(event["uid"], None)
                self._motion.pop(event["uid"], None)
                self._burst(tile, 12, (200, 120, 90))
                if self.sfx:
                    self.sfx.play("kill")

        elif kind == "spawn":
            tile = tuple(event["at"])
            self.view.units[event["uid"]] = {
                "uid": event["uid"], "owner": event["owner"],
                "code": event["code"], "hp": event["hp"],
                "x": tile[0], "y": tile[1]}
            self._revealed.add(tile)
            if self.sfx:
                self.sfx.play("spawn")

        elif kind in ("found", "ready"):
            tile = tuple(event["at"])
            self._revealed.add(tile)
            if kind == "ready" and self.sfx:
                self.sfx.play("ready")

        elif kind == "capture":
            tile = tuple(event["at"])
            self.view.node_owner[tile] = event["pid"]
            self._revealed.add(tile)
            self._burst(tile, 10, (230, 200, 90), spread=30)
            self._floaters.append(Floater(*self._pos(tile), "CAPTURED",
                                          UI_ACCENT))
            if self.sfx:
                self.sfx.play("capture")

        elif kind == "block":
            tile = tuple(event["at"])
            self._floaters.append(Floater(*self._pos(tile), "blocked", UI_TEXT,
                                          life=0.8))

        elif kind == "income":
            self.view.supply = event.get("total", self.view.supply)

    def _pos(self, tile) -> tuple[int, int]:
        return self.renderer.board.centre(tile)

    def _burst(self, tile, count: int, colour, spread: float = 46.0) -> None:
        cx, cy = self._pos(tile)
        for _ in range(count):
            angle = self._rng.uniform(0, math.tau)
            speed = self._rng.uniform(spread * 0.3, spread)
            self._puffs.append(Puff(cx, cy, math.cos(angle) * speed,
                                    math.sin(angle) * speed - 12,
                                    self._rng.uniform(0.3, 0.8), colour,
                                    size=self._rng.choice((1, 2))))

    # -- drawing -----------------------------------------------------------
    def unit_pixel(self, unit: dict):
        """Where to draw a unit right now, interpolating an in-progress step."""
        motion = self._motion.get(unit["uid"])
        if motion is None:
            return None
        (fx, fy), (tx, ty), start = motion
        progress = max(0.0, min(1.0, self.beat - start))
        if progress >= 1.0:
            return None
        board = self.renderer.board
        px = board.ox + (fx + (tx - fx) * progress) * TILE + TILE // 2
        py = board.oy + (fy + (ty - fy) * progress) * TILE + TILE // 2
        return (int(px), int(py))

    def extra_visible(self) -> set:
        """Tiles the replay has revealed, so fog lifts as the turn unfolds."""
        return self._revealed

    def draw_effects(self, dest: pygame.Surface) -> None:
        for tracer in self._tracers:
            fade = 1.0 - tracer.age / tracer.life
            pygame.draw.line(dest, shade(tracer.colour, 0.5 + 0.5 * fade),
                             tracer.a, tracer.b, 1)
        for puff in self._puffs:
            fade = 1.0 - puff.age / puff.life
            dest.fill(shade(puff.colour, 0.35 + 0.65 * fade),
                      (int(puff.x), int(puff.y), puff.size, puff.size))
        for floater in self._floaters:
            fade = 1.0 - (floater.age / floater.life) ** 2
            ui.draw_text(dest, floater.text, int(floater.x), int(floater.y), 14,
                         shade(floater.colour, 0.35 + 0.65 * fade),
                         anchor="center", shadow=True)

    def progress(self) -> float:
        return max(0.0, min(1.0, self.beat / max(1.0, self.total)))
