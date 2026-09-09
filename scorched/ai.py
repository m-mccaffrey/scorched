"""Computer players.

The bots aim the way a person eventually learns to: start from the textbook
ballistic solution, fire a mental tracer, see how far off it lands, and nudge.
Three or four refinement passes get within a few pixels, which is why the top
skill levels feel genuinely uncomfortable to play against.

Difficulty is then applied as *error added on purpose*, not as a worse
algorithm -- a novice bot is a good shot with shaky hands, which misses in
believable ways instead of in bizarre ones.
"""

from __future__ import annotations

import math
import random

from . import weapons as W
from .physics import POWER_SCALE, trace

#: (angle jitter in degrees, power jitter as a fraction, aim-point scatter px)
SKILLS = {
    "novice":   (3.4, 0.055, 19.0),
    "moderate": (2.2, 0.035, 13.0),
    "expert":   (1.0, 0.015, 6.0),
    "cyborg":   (0.0, 0.0, 0.0),
}
SKILL_ORDER = ("novice", "moderate", "expert", "cyborg")


def _ballistic(dx: float, dy_up: float, angle_deg: float, gravity: float) -> float | None:
    """Muzzle power needed to pass through (dx, dy_up) at the given angle.

    ``dy_up`` is positive when the target sits *above* the shooter, which is the
    opposite of screen y -- the caller converts.  Returns None when no speed can
    make that angle work (target is behind the barrel, or above the asymptote).
    """
    rad = math.radians(angle_deg)
    cos = math.cos(rad)
    if abs(cos) < 1e-6 or dx == 0:
        return None
    denom = 2.0 * cos * cos * (dx * math.tan(rad) - dy_up)
    if denom <= 0:
        return None
    v_sq = gravity * dx * dx / denom
    if v_sq <= 0:
        return None
    return math.sqrt(v_sq) / POWER_SCALE


class BotBrain:
    """Chooses a weapon and an aim for one bot on one turn."""

    def __init__(self, skill: str = "moderate", rng: random.Random | None = None) -> None:
        self.skill = skill if skill in SKILLS else "moderate"
        self.rng = rng or random.Random()

    # -- top level -------------------------------------------------------
    def take_turn(self, game, me) -> dict:
        """Return the action to perform: weapon, angle, power, and extras."""
        target = self._pick_target(game, me)
        action = {"shield": None, "weapon": me.weapon, "angle": me.angle,
                  "power": me.power}

        if me.hp <= 55:
            action["shield"] = self._pick_shield(me)

        if target is None:
            action["angle"] = self.rng.randint(30, 150)
            action["power"] = self.rng.randint(300, 700)
            return action

        action["weapon"] = self._pick_weapon(me, target)
        angle, power = self._aim(game, me, target)
        action["angle"] = angle
        action["power"] = power
        return action

    # -- decisions -------------------------------------------------------
    def _pick_target(self, game, me):
        enemies = [p for p in game.players.values() if p.alive and p.pid != me.pid]
        if not enemies:
            return None

        def desirability(p):
            distance = abs(p.x - me.x)
            # Wounded and close is best; the weights are gentle so bots do not
            # all gang up on one player every single turn.
            return p.hp * 1.4 + distance * 0.25 + self.rng.uniform(0, 45)

        return min(enemies, key=desirability)

    def _pick_shield(self, me):
        for code in ("hshld", "shld"):
            if me.inv.item_count(code) > 0 and me.shield_hp <= 0:
                return code
        return None

    def _pick_weapon(self, me, target) -> str:
        usable = [W.WEAPON_BY_CODE[c] for c in me.inv.available_weapons()]
        usable = [w for w in usable if w.damage > 0 or w.kind == "roller"]
        if not usable:
            return "bmis"
        # Do not waste a Nuke finishing off a tank on 8 hp, and do not plink at
        # a healthy one with Baby Missiles if something better is in the rack.
        wanted = 3 if target.hp > 70 else (2 if target.hp > 35 else 1)
        if target.shield_hp > 0:
            wanted = 3
        buried = target.y - me.y > 40
        pool = [w for w in usable if w.tier <= wanted]
        if buried:
            rollers = [w for w in usable if w.kind == "roller"]
            if rollers and self.rng.random() < 0.6:
                return max(rollers, key=lambda w: w.tier).code
        if not pool:
            pool = usable
        best = max(w.tier for w in pool)
        top = [w for w in pool if w.tier == best]
        return self.rng.choice(top).code

    # -- aiming ----------------------------------------------------------
    def _aim(self, game, me, target) -> tuple[int, int]:
        jitter_angle, jitter_power, scatter = SKILLS[self.skill]
        aim_x = target.x + self.rng.uniform(-scatter, scatter)
        aim_y = target.y - 4

        best: tuple[float, int, int] | None = None
        for base_angle in self._candidate_angles(me, target):
            solved = self._solve(game, me, aim_x, aim_y, base_angle)
            if solved is None:
                continue
            error, angle, power = solved
            if best is None or error < best[0]:
                best = (error, angle, power)
            if error < 6.0:
                break

        if best is None:
            # No solution found -- lob it hopefully in the right direction.
            toward_right = target.x > me.x
            angle = self.rng.randint(35, 60) if toward_right else self.rng.randint(120, 145)
            return angle, self.rng.randint(400, 800)

        _err, angle, power = best
        angle += self.rng.gauss(0, jitter_angle)
        power *= 1.0 + self.rng.gauss(0, jitter_power)
        return (max(1, min(179, int(round(angle)))),
                max(50, min(1000, int(round(power)))))

    def _candidate_angles(self, me, target) -> list[float]:
        """Angles worth trying, flipped to face the target."""
        toward_right = target.x >= me.x
        base = [45.0, 55.0, 35.0, 65.0, 75.0, 25.0, 82.0]
        return [a if toward_right else 180.0 - a for a in base]

    def _solve(self, game, me, aim_x: float, aim_y: float,
               angle: float) -> tuple[float, int, int] | None:
        """Iteratively correct for wind and terrain until the shot lands close.

        Each pass fires a simulated tracer, measures the miss, and shifts the
        *virtual* aim point by the opposite of the error.  Converges fast
        because the miss is very nearly linear in the aim offset.
        """
        gravity = game.settings.gravity
        tanks = list(game.players.values())
        offset_x = 0.0
        best: tuple[float, int, int] | None = None

        for _ in range(4):
            dx = (aim_x + offset_x) - me.x
            dy_up = me.y - aim_y
            power = _ballistic(dx, dy_up, angle, gravity)
            if power is None or power > 1000:
                # Compensate roughly for wind pushing us short or long, then
                # give up on this angle if it still cannot reach.
                if power is None:
                    return None
                power = 1000.0
            power = max(50.0, power)
            impact = trace(game.terrain, tanks, me, angle, power, game.wind,
                           gravity, game.settings.wall_mode)
            if impact is None:
                offset_x -= math.copysign(60.0, dx)
                continue
            error = math.hypot(impact[0] - aim_x, impact[1] - aim_y)
            candidate = (error, int(round(angle)), int(round(power)))
            if best is None or error < best[0]:
                best = candidate
            if error < 5.0:
                break
            offset_x += (aim_x - impact[0]) * 0.9
        return best

    # -- shopping --------------------------------------------------------
    def shop(self, game, me) -> None:
        """Spend the round's winnings. Bots buy defence first, then firepower."""
        if me.inv.item_count("shld") + me.inv.item_count("hshld") <= 0:
            for code in ("hshld", "shld"):
                if game.buy(me, code, 1):
                    break
        if me.inv.item_count("rep") <= 0 and self.rng.random() < 0.6:
            game.buy(me, "rep", 1)
        if me.inv.item_count("para") <= 0 and self.rng.random() < 0.35:
            game.buy(me, "para", 1)

        affordable = [w for w in W.WEAPONS
                      if not w.unlimited and w.damage > 0 and w.price <= me.cash]
        guard = 0
        while affordable and me.cash > 2000 and guard < 12:
            guard += 1
            # Weight toward the strongest thing in reach, with a taste for
            # variety so every bot's rack does not look identical.
            affordable.sort(key=lambda w: w.price)
            pick = affordable[-1] if self.rng.random() < 0.6 else self.rng.choice(affordable)
            if not game.buy(me, pick.code, 1):
                break
            affordable = [w for w in affordable if w.price <= me.cash]
        me.done_buying = True
