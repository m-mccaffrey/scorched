"""Projectile simulation.

The server runs a whole shot to completion the instant the player fires, and
emits a *timeline*: a list of events, each stamped with the animation frame it
happens on.  Clients receive that timeline and play it back locally.

That design is the reason cross-platform play here is trouble-free.  Nothing is
simulated twice, so nothing can disagree: a Raspberry Pi and a Windows PC are
not racing each other's floating-point units, they are playing the same short
film.  It also means network jitter shows up as a slightly later *start* to the
animation rather than as stutter during it, and a 200 ms hiccup mid-flight is
invisible.

Frames are 1/60 s.  Coordinates are floats internally and rounded to integers on
the wire -- a pixel of rounding is imperceptible and it halves the payload.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .terrain import Terrain
from .weapons import WEAPON_BY_CODE, Weapon

# -- tuning -----------------------------------------------------------------
POWER_SCALE = 0.015       # power 1000 -> 15 px/frame muzzle velocity
WIND_SCALE = 0.0003       # wind 100 -> ~150 px of drift over a long arc
DEFAULT_GRAVITY = 0.15
MAX_FRAMES = 1500         # 25 s; a shot that long has gone somewhere silly
TANK_RADIUS = 7           # collision half-extent of a tank hull
SHIELD_RADIUS = 15
ROLL_SPEED = 3            # px/frame for rollers
FALL_SAFE = 12            # free fall distance before it hurts
FALL_DAMAGE = 0.9         # hp per pixel beyond FALL_SAFE

WALL_MODES = ("none", "rebound", "wrap")


def muzzle_velocity(angle_deg: float, power: float) -> tuple[float, float]:
    """Convert the classic angle/power pair into a velocity vector.

    Angle is degrees counter-clockwise from due east, so 90 is straight up and
    the whole 0..180 range points somewhere useful.
    """
    rad = math.radians(angle_deg)
    speed = power * POWER_SCALE
    return math.cos(rad) * speed, -math.sin(rad) * speed


def explosion_frames(radius: int) -> int:
    """How long the client will spend drawing a blast of this size."""
    return 10 + radius // 3


@dataclass
class _Proj:
    x: float
    y: float
    vx: float
    vy: float
    weapon: Weapon
    owner: int
    start: int
    pts: list = field(default_factory=list)
    mode: str = "fly"          # "fly" | "roll"
    hops: int = 0              # leapfrog jumps remaining
    bounces: int = 0           # funky bomblet bounces remaining
    fuse: int = -1             # frames until forced detonation (-1 = none)
    split: bool = False        # MIRV: already split?
    damage: int = 0
    radius: int = 0
    grace: int = 4             # frames during which we ignore the firer


class ShotResult:
    """Everything the rest of the game needs to know about one shot."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.frames: int = 0
        self.damage_dealt: dict[int, int] = {}   # victim pid -> total damage
        self.killed: list[int] = []
        self.terrain_ops: list[tuple] = []

    def to_wire(self) -> dict:
        return {"events": self.events, "frames": self.frames}


class Simulation:
    """Runs one shot against a live world and records what happened."""

    def __init__(self, terrain: Terrain, tanks: list, wind: float,
                 gravity: float = DEFAULT_GRAVITY, wall: str = "none",
                 seed: int | None = None) -> None:
        self.terrain = terrain
        self.tanks = tanks                    # objects with pid/x/y/alive/hp/...
        self.wind = wind
        self.gravity = gravity
        self.wall = wall if wall in WALL_MODES else "none"
        self.rng = random.Random(seed)
        self.frame = 0
        self.result = ShotResult()
        self._active: list[_Proj] = []

    # -- public ----------------------------------------------------------
    def fire(self, shooter, angle: float, power: float, weapon_code: str) -> ShotResult:
        weapon = WEAPON_BY_CODE.get(weapon_code) or WEAPON_BY_CODE["bmis"]
        vx, vy = muzzle_velocity(angle, power)
        # Start at the muzzle, not the hull centre, so a point-blank shot does
        # not immediately collide with the tank that fired it.
        rad = math.radians(angle)
        sx = shooter.x + math.cos(rad) * 11.0
        sy = shooter.y - 6.0 - math.sin(rad) * 11.0
        proj = _Proj(x=sx, y=sy, vx=vx, vy=vy, weapon=weapon, owner=shooter.pid,
                     start=0, damage=weapon.damage, radius=weapon.radius)
        if weapon.kind == "leapfrog":
            proj.hops = weapon.children
        self._active.append(proj)
        self._run()
        return self.result

    # -- main loop -------------------------------------------------------
    def _run(self) -> None:
        tail = 0
        while self._active and self.frame < MAX_FRAMES:
            for proj in list(self._active):
                self._step(proj)
            self.frame += 1
        # Anything still airborne when we hit the cap simply fizzles out.
        for proj in list(self._active):
            self._retire(proj)
        for ev in self.result.events:
            if ev["e"] == "explode":
                tail = max(tail, ev["f"] + explosion_frames(ev["r"]))
            elif ev["e"] == "traj":
                tail = max(tail, ev["f"] + len(ev["pts"]))
        self.result.frames = max(self.frame, tail) + 6

    def _step(self, proj: _Proj) -> None:
        if proj.mode == "roll":
            self._step_roll(proj)
            return

        if proj.grace > 0:
            proj.grace -= 1

        # Integrate with sub-steps so a fast round cannot tunnel through a
        # tank or a thin ridge.  Trajectory points are still recorded once per
        # frame -- the client only needs one position per frame to draw.
        speed = math.hypot(proj.vx, proj.vy)
        steps = max(1, min(8, int(speed / 3.0) + 1))
        inv = 1.0 / steps
        for _ in range(steps):
            proj.vy += self.gravity * inv
            proj.vx += self.wind * WIND_SCALE * inv
            proj.x += proj.vx * inv
            proj.y += proj.vy * inv
            if self._wrap_or_bounce(proj):
                return                     # left the world entirely
            hit = self._collision(proj)
            if hit is not None:
                proj.pts.append((round(proj.x), round(proj.y)))
                self._impact(proj, hit)
                return

        proj.pts.append((round(proj.x), round(proj.y)))

        # MIRVs open at the top of the arc, which is what makes them scary.
        if proj.weapon.kind == "mirv" and not proj.split and proj.vy >= 0:
            proj.split = True
            self._retire(proj)
            self._spawn_mirv(proj)
            return

        if proj.fuse > 0:
            proj.fuse -= 1
            if proj.fuse == 0:
                self._impact(proj, ("air", None))

    def _step_roll(self, proj: _Proj) -> None:
        """A roller crawling along the surface looking for a low spot."""
        terrain = self.terrain
        direction = 1 if proj.vx >= 0 else -1
        for _ in range(ROLL_SPEED):
            nx = proj.x + direction
            if nx < 1 or nx >= terrain.width - 1:
                self._impact(proj, ("terrain", None))
                return
            here = terrain.surface(int(proj.x))
            there = terrain.surface(int(nx))
            if there < here - 1:
                # Uphill: the roller has found its resting place.
                self._impact(proj, ("terrain", None))
                return
            proj.x = nx
            proj.y = there
            tank = self._tank_at(proj, proj.x, proj.y)
            if tank is not None:
                self._impact(proj, ("tank", tank))
                return
        proj.pts.append((round(proj.x), round(proj.y)))
        if len(proj.pts) > 400:            # a roller on a perfect plain
            self._impact(proj, ("terrain", None))

    # -- world interaction ------------------------------------------------
    def _wrap_or_bounce(self, proj: _Proj) -> bool:
        w = self.terrain.width
        if self.wall == "wrap":
            if proj.x < 0:
                proj.x += w
            elif proj.x >= w:
                proj.x -= w
            return False
        if self.wall == "rebound":
            if proj.x < 0:
                proj.x = -proj.x
                proj.vx = -proj.vx
            elif proj.x >= w:
                proj.x = 2 * w - proj.x - 1
                proj.vx = -proj.vx
            return False
        # "none": off the side is gone, but only once it is clearly away and
        # falling -- a shot that clips the edge on the way up may come back.
        if proj.x < -160 or proj.x > w + 160:
            self._retire(proj)
            return True
        return False

    def _collision(self, proj: _Proj):
        terrain = self.terrain
        x, y = proj.x, proj.y
        if y >= terrain.height_limit:
            # The floor of the world. Detonate rather than vanish, so digging a
            # pit through the map does not create a projectile black hole.
            proj.y = terrain.height_limit - 1
            return ("terrain", None)
        if y < -600:
            return None                     # still climbing into the sky
        tank = self._tank_at(proj, x, y)
        if tank is not None:
            return ("tank", tank)
        if terrain.is_solid(int(x), int(y)):
            return ("terrain", None)
        return None

    def _tank_at(self, proj: _Proj, x: float, y: float):
        for tank in self.tanks:
            if not tank.alive:
                continue
            if tank.pid == proj.owner and proj.grace > 0:
                continue
            dx = x - tank.x
            dy = y - (tank.y - 5)
            reach = SHIELD_RADIUS if tank.shield_hp > 0 else TANK_RADIUS
            if dx * dx + dy * dy <= reach * reach:
                return tank
        return None

    # -- detonation -------------------------------------------------------
    def _impact(self, proj: _Proj, hit) -> None:
        self._retire(proj)
        kind = proj.weapon.kind

        if kind == "tracer":
            self.result.events.append({
                "f": self.frame, "e": "puff", "x": round(proj.x), "y": round(proj.y)
            })
            return

        if kind == "dirt":
            self._terrain_op("dirt", round(proj.x), round(proj.y), proj.radius)
            self.result.events.append({
                "f": self.frame, "e": "dirtpuff", "x": round(proj.x),
                "y": round(proj.y), "r": proj.radius,
            })
            self._settle_tanks()
            return

        if kind == "digger":
            self._terrain_op("crater", round(proj.x), round(proj.y), proj.radius)
            self.result.events.append({
                "f": self.frame, "e": "explode", "x": round(proj.x),
                "y": round(proj.y), "r": proj.radius, "k": "dig",
            })
            self._settle_tanks()
            return

        if kind == "roller" and proj.mode == "fly" and hit[0] == "terrain":
            # Land, then roll: pick the downhill direction from the local slope.
            proj.mode = "roll"
            proj.grace = 0
            slope = self.terrain.slope(int(proj.x))
            if slope == 0:
                proj.vx = 1.0 if proj.vx >= 0 else -1.0
            else:
                proj.vx = 1.0 if slope > 0 else -1.0
            proj.y = self.terrain.surface(int(proj.x))
            self._active.append(proj)
            proj.start = self.frame
            proj.pts = []
            return

        self._explode(round(proj.x), round(proj.y), proj.radius, proj.damage,
                      proj.owner, kind)

        if kind == "leapfrog" and proj.hops > 0:
            child = _Proj(
                x=proj.x, y=proj.y - 6, vx=proj.vx * 0.35 or 1.5,
                vy=-(abs(proj.vy) * 0.30 + 2.0), weapon=proj.weapon, owner=proj.owner,
                start=self.frame, hops=proj.hops - 1, damage=proj.damage,
                radius=proj.radius, grace=0,
            )
            self._active.append(child)
        elif kind == "funky":
            self._spawn_funky(proj)

    def _explode(self, x: int, y: int, radius: int, damage: int, owner: int,
                 kind: str) -> None:
        self.result.events.append({
            "f": self.frame, "e": "explode", "x": x, "y": y, "r": radius, "k": kind,
        })
        if damage > 0:
            self._apply_damage(x, y, radius, damage, owner)
        self._terrain_op("crater", x, y, radius)
        self._settle_tanks()

    def _apply_damage(self, x: int, y: int, radius: int, damage: int,
                      owner: int) -> None:
        reach = radius + TANK_RADIUS
        for tank in self.tanks:
            if not tank.alive:
                continue
            dist = math.hypot(x - tank.x, y - (tank.y - 5))
            if dist > reach:
                continue
            # Falloff is deliberately shallow near the centre and steep at
            # the rim: a hit that looks like a hit should hurt like one, while
            # a shell that merely lands nearby still only scratches the paint.
            amount = int(round(damage * (1.0 - (dist / reach) ** 1.7)))
            if amount <= 0:
                continue
            self._hurt(tank, amount, owner)

    def _hurt(self, tank, amount: int, owner: int) -> None:
        absorbed = 0
        if tank.shield_hp > 0:
            absorbed = min(tank.shield_hp, amount)
            tank.shield_hp -= absorbed
            amount -= absorbed
            self.result.events.append({
                "f": self.frame, "e": "shield", "p": tank.pid,
                "sp": tank.shield_hp, "d": absorbed,
            })
        if amount <= 0:
            return
        tank.hp = max(0, tank.hp - amount)
        self.result.damage_dealt[tank.pid] = (
            self.result.damage_dealt.get(tank.pid, 0) + amount
        )
        self.result.events.append({
            "f": self.frame, "e": "hp", "p": tank.pid, "hp": tank.hp, "d": amount,
        })
        if tank.hp <= 0 and tank.alive:
            tank.alive = False
            self.result.killed.append(tank.pid)
            self.result.events.append({
                "f": self.frame, "e": "death", "p": tank.pid,
                "x": round(tank.x), "y": round(tank.y),
            })
            # A dying tank takes a bite out of the hill with it.
            self._terrain_op("crater", round(tank.x), round(tank.y), 22)
            self.result.events.append({
                "f": self.frame, "e": "explode", "x": round(tank.x),
                "y": round(tank.y), "r": 22, "k": "death",
            })

    def _settle_tanks(self) -> None:
        """Drop any tank left hanging in the air after the ground moved."""
        for tank in self.tanks:
            if not tank.alive:
                continue
            ground = self.terrain.surface(int(tank.x))
            if ground <= tank.y + 1:
                if ground < tank.y:
                    tank.y = ground        # dirt piled underneath: ride up
                    self.result.events.append({
                        "f": self.frame, "e": "fall", "p": tank.pid,
                        "x": round(tank.x), "y": round(tank.y), "d": 0,
                    })
                continue
            drop = ground - tank.y
            tank.y = ground
            damage = 0
            if drop > FALL_SAFE:
                if tank.parachutes > 0:
                    tank.parachutes -= 1
                    self.result.events.append({
                        "f": self.frame, "e": "chute", "p": tank.pid,
                    })
                else:
                    damage = int((drop - FALL_SAFE) * FALL_DAMAGE)
            self.result.events.append({
                "f": self.frame, "e": "fall", "p": tank.pid,
                "x": round(tank.x), "y": round(tank.y), "d": drop,
            })
            if damage > 0:
                self._hurt(tank, damage, tank.pid)

    def _terrain_op(self, op: str, x: int, y: int, r: int) -> None:
        self.terrain.apply(op, x, y, r)
        self.result.terrain_ops.append((op, x, y, r))
        self.result.events.append({
            "f": self.frame, "e": "terrain", "op": op, "x": x, "y": y, "r": r,
        })

    # -- sub-munitions -----------------------------------------------------
    def _spawn_mirv(self, parent: _Proj) -> None:
        n = max(2, parent.weapon.children)
        for i in range(n):
            spread = (i - (n - 1) / 2.0) * 0.9
            child = _Proj(
                x=parent.x, y=parent.y, vx=parent.vx + spread,
                vy=parent.vy + self.rng.uniform(-0.3, 0.3),
                weapon=parent.weapon, owner=parent.owner, start=self.frame,
                damage=parent.damage, radius=parent.radius, grace=0, split=True,
            )
            self._active.append(child)
        self.result.events.append({
            "f": self.frame, "e": "split", "x": round(parent.x),
            "y": round(parent.y),
        })

    def _spawn_funky(self, parent: _Proj) -> None:
        n = max(2, parent.weapon.children)
        for _ in range(n):
            child = _Proj(
                x=parent.x, y=parent.y - 4,
                vx=self.rng.uniform(-4.5, 4.5),
                vy=self.rng.uniform(-6.5, -2.0),
                weapon=parent.weapon, owner=parent.owner, start=self.frame,
                damage=max(8, parent.damage // 2), radius=max(8, parent.radius - 4),
                grace=0, fuse=self.rng.randint(18, 46), split=True,
            )
            # Bomblets are their own little HE rounds once the fuse burns down.
            child.weapon = WEAPON_BY_CODE["bmis"]
            self._active.append(child)

    def _retire(self, proj: _Proj) -> None:
        if proj in self._active:
            self._active.remove(proj)
        if proj.pts:
            self.result.events.append({
                "f": proj.start, "e": "traj", "pts": [list(p) for p in proj.pts],
                "k": proj.weapon.kind if proj.mode == "fly" else "roll",
            })


def trace(terrain: Terrain, tanks: list, shooter, angle: float, power: float,
          wind: float, gravity: float = DEFAULT_GRAVITY,
          wall: str = "none") -> tuple[float, float] | None:
    """Where would this shot land?  Used by the AI; changes nothing.

    Runs the same integrator against a throwaway copy of the terrain so that
    the bots' aiming practice never actually moves any dirt.
    """
    scratch = Terrain(terrain.height, terrain.height_limit)
    vx, vy = muzzle_velocity(angle, power)
    rad = math.radians(angle)
    x = shooter.x + math.cos(rad) * 11.0
    y = shooter.y - 6.0 - math.sin(rad) * 11.0
    frames = 0
    while frames < MAX_FRAMES:
        speed = math.hypot(vx, vy)
        steps = max(1, min(8, int(speed / 3.0) + 1))
        inv = 1.0 / steps
        for _ in range(steps):
            vy += gravity * inv
            vx += wind * WIND_SCALE * inv
            x += vx * inv
            y += vy * inv
            if wall == "wrap":
                if x < 0:
                    x += scratch.width
                elif x >= scratch.width:
                    x -= scratch.width
            elif wall == "rebound":
                if x < 0:
                    x, vx = -x, -vx
                elif x >= scratch.width:
                    x, vx = 2 * scratch.width - x - 1, -vx
            elif x < -160 or x > scratch.width + 160:
                return None
            if y >= scratch.height_limit:
                return (x, float(scratch.height_limit - 1))
            if y > -600 and scratch.is_solid(int(x), int(y)):
                return (x, y)
        frames += 1
    return None
