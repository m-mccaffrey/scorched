"""Playback of a shot timeline, plus the particles that sell it.

The server hands over a list of events, each stamped with the frame it happens
on.  This module walks that list one frame at a time and turns it into light,
noise and moving dirt.  Because the timeline is fixed before the first frame is
drawn, playback cannot desync -- a client that stalls simply finishes late, and
the server's grace period covers it.
"""

from __future__ import annotations

import math
import random

import pygame

from ..physics import explosion_frames
from ..terrain import PLAY_H, WORLD_W
from .palette import DIG_RAMP, FIRE_RAMP, SHIELD, UI_ACCENT, shade
from . import ui

TRAIL_LEN = 14


class Particle:
    __slots__ = ("x", "y", "vx", "vy", "life", "age", "color", "size", "gravity")

    def __init__(self, x, y, vx, vy, life, color, size=1, gravity=0.14):
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.life = life
        self.age = 0
        self.color = color
        self.size = size
        self.gravity = gravity

    def update(self) -> bool:
        self.age += 1
        self.vy += self.gravity
        self.vx *= 0.99
        self.x += self.vx
        self.y += self.vy
        return self.age < self.life


class FloatingText:
    __slots__ = ("x", "y", "text", "color", "age", "life")

    def __init__(self, x, y, text, color, life=48):
        self.x, self.y, self.text, self.color = x, y, text, color
        self.age = 0
        self.life = life

    def update(self) -> bool:
        self.age += 1
        self.y -= 0.55
        return self.age < self.life


class Explosion:
    __slots__ = ("x", "y", "radius", "age", "life", "ramp")

    def __init__(self, x, y, radius, kind="he"):
        self.x, self.y, self.radius = x, y, radius
        self.age = 0
        self.life = explosion_frames(radius)
        self.ramp = DIG_RAMP if kind == "dig" else FIRE_RAMP

    def update(self) -> bool:
        self.age += 1
        return self.age <= self.life

    def draw(self, dest: pygame.Surface) -> None:
        t = self.age / max(1, self.life)
        # Grow fast, hold, then let the cooler ring linger.
        grow = 1.0 - (1.0 - min(1.0, t * 1.9)) ** 2
        outer = max(1, int(self.radius * grow))
        steps = len(self.ramp)
        for i in range(steps):
            frac = 1.0 - i / steps
            r = int(outer * frac)
            if r < 1:
                continue
            shade_index = min(steps - 1, int(i + t * (steps - 1)))
            pygame.draw.circle(dest, self.ramp[shade_index],
                               (int(self.x), int(self.y)), r)


class ShotPlayer:
    """Replays one shot's timeline against the local view of the world."""

    def __init__(self, payload: dict, state, renderer, sfx=None) -> None:
        self.seq = payload.get("seq", 0)
        self.total = payload.get("frames", 60)
        self.events = sorted(payload.get("events", []), key=lambda e: e.get("f", 0))
        self.state = state
        self.renderer = renderer
        self.sfx = sfx
        self.frame = 0
        self.index = 0
        self.finished = False
        self.shake = 0.0
        self._trails: list[dict] = []
        self._particles: list[Particle] = []
        self._texts: list[FloatingText] = []
        self._explosions: list[Explosion] = []
        self._rng = random.Random(self.seq)

    # -- update ------------------------------------------------------------
    def update(self) -> bool:
        """Advance one frame. Returns False once everything has played out."""
        while self.index < len(self.events) and self.events[self.index]["f"] <= self.frame:
            self._apply(self.events[self.index])
            self.index += 1

        for trail in self._trails:
            trail["cursor"] += 1
        self._trails = [t for t in self._trails
                        if t["cursor"] < len(t["pts"]) + TRAIL_LEN]

        self._explosions = [e for e in self._explosions if e.update()]
        self._particles = [p for p in self._particles if p.update()]
        self._texts = [t for t in self._texts if t.update()]
        self.shake *= 0.86

        self.frame += 1
        done = (self.frame > self.total and not self._explosions
                and not self._trails and self.index >= len(self.events))
        if done:
            self.finished = True
        return not self.finished

    def _apply(self, event: dict) -> None:
        kind = event["e"]
        if kind == "traj":
            self._trails.append({"pts": event["pts"], "cursor": 0,
                                 "k": event.get("k", "he")})
            if self.sfx:
                self.sfx.play("launch")
        elif kind == "explode":
            self._boom(event)
        elif kind == "terrain":
            self.state.terrain.apply(event["op"], event["x"], event["y"], event["r"])
            self.renderer.refresh_terrain()
        elif kind == "hp":
            self._on_damage(event)
        elif kind == "shield":
            self._on_shield(event)
        elif kind == "fall":
            self._on_fall(event)
        elif kind == "death":
            self._on_death(event)
        elif kind == "puff":
            self._puff(event["x"], event["y"], (220, 220, 230), 10)
        elif kind == "dirtpuff":
            self._puff(event["x"], event["y"], (150, 124, 78), 26)
            if self.sfx:
                self.sfx.play("thud")
        elif kind == "split":
            self._puff(event["x"], event["y"], UI_ACCENT, 14)
            if self.sfx:
                self.sfx.play("split")
        elif kind == "chute":
            player = self.state.players.get(event["p"])
            if player:
                self._texts.append(FloatingText(player["x"], player["y"] - 18,
                                                "CHUTE", SHIELD))

    # -- event handlers -----------------------------------------------------
    def _boom(self, event: dict) -> None:
        x, y, radius = event["x"], event["y"], event["r"]
        self._explosions.append(Explosion(x, y, radius, event.get("k", "he")))
        self.shake = min(9.0, self.shake + radius * 0.14)
        if self.sfx:
            self.sfx.play("boom", radius)
        debris = min(46, 8 + radius)
        crust = self.renderer.ground[0]
        for _ in range(debris):
            angle = self._rng.uniform(0, math.tau)
            speed = self._rng.uniform(0.8, 1.0 + radius * 0.13)
            self._particles.append(Particle(
                x, y, math.cos(angle) * speed, math.sin(angle) * speed - 1.2,
                self._rng.randint(18, 46),
                crust if self._rng.random() < 0.6 else FIRE_RAMP[self._rng.randrange(4)],
                size=1 if self._rng.random() < 0.7 else 2))

    def _on_damage(self, event: dict) -> None:
        player = self.state.players.get(event["p"])
        if player is None:
            return
        player["hp"] = event["hp"]
        # Nudge the number sideways: on a killing blow it would otherwise rise
        # through the "DESTROYED" banner spawned on the same frame.
        drift = self._rng.choice((-18, 18))
        self._texts.append(FloatingText(player["x"] + drift, player["y"] - 22,
                                        f"-{event['d']}", (255, 120, 96)))

    def _on_shield(self, event: dict) -> None:
        player = self.state.players.get(event["p"])
        if player is None:
            return
        player["shield"] = event["sp"]
        self._texts.append(FloatingText(player["x"] + 12, player["y"] - 26,
                                        f"-{event['d']}", SHIELD, life=34))
        if self.sfx:
            self.sfx.play("shield")

    def _on_fall(self, event: dict) -> None:
        player = self.state.players.get(event["p"])
        if player is None:
            return
        player["x"], player["y"] = event["x"], event["y"]

    def _on_death(self, event: dict) -> None:
        player = self.state.players.get(event["p"])
        if player is None:
            return
        player["alive"] = False
        player["hp"] = 0
        self._texts.append(FloatingText(event["x"], event["y"] - 30,
                                        "DESTROYED", (255, 90, 70), life=70))

    def _puff(self, x: int, y: int, color, count: int) -> None:
        for _ in range(count):
            angle = self._rng.uniform(0, math.tau)
            speed = self._rng.uniform(0.3, 1.8)
            self._particles.append(Particle(
                x, y, math.cos(angle) * speed, math.sin(angle) * speed,
                self._rng.randint(14, 30), color, gravity=0.06))

    # -- draw ---------------------------------------------------------------
    def draw(self, dest: pygame.Surface) -> None:
        for trail in self._trails:
            self._draw_trail(dest, trail)
        for explosion in self._explosions:
            explosion.draw(dest)
        for particle in self._particles:
            if 0 <= particle.x < WORLD_W and 0 <= particle.y < PLAY_H:
                fade = 1.0 - particle.age / particle.life
                color = shade(particle.color, 0.35 + 0.65 * fade)
                dest.fill(color, (int(particle.x), int(particle.y),
                                  particle.size, particle.size))
        for label in self._texts:
            fade = 1.0 - (label.age / label.life) ** 2
            color = shade(label.color, 0.3 + 0.7 * fade)
            ui.draw_text(dest, label.text, int(label.x), int(label.y), 15, color,
                         anchor="center", shadow=True)

    def _draw_trail(self, dest: pygame.Surface, trail: dict) -> None:
        pts = trail["pts"]
        cursor = trail["cursor"]
        head = min(cursor, len(pts) - 1)
        start = max(0, cursor - TRAIL_LEN)
        smoke = trail["k"] not in ("roll",)
        for i in range(start, head):
            x, y = pts[i]
            if not (0 <= x < WORLD_W and 0 <= y < PLAY_H):
                continue
            age = (head - i) / TRAIL_LEN
            if smoke:
                grey = int(200 * (1.0 - age))
                dest.fill((grey, grey, max(grey - 20, 0)), (x, y, 1, 1))
        if cursor < len(pts):
            x, y = pts[cursor]
            if 0 <= x < WORLD_W and 0 <= y < PLAY_H:
                pygame.draw.circle(dest, (255, 240, 180), (x, y), 2)
                dest.set_at((x, y), (255, 255, 255))

    def offset(self) -> tuple[int, int]:
        """Screen shake, applied by the caller when blitting the world."""
        if self.shake < 0.4:
            return (0, 0)
        return (self._rng.randint(-int(self.shake), int(self.shake)),
                self._rng.randint(-int(self.shake), int(self.shake)) // 2)
