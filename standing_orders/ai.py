"""Computer commanders.

The bots play by the same rules as everyone else, fog of war included: they
plan from what their own units can actually see, not from the server's full
picture. A bot that cheats is a bad opponent -- it makes scouting pointless and
teaches the family the wrong lessons about the game.

Difficulty is expressed as *restraint and discipline*, not as extra
information: a Novice dribbles units forward one at a time and ignores what it
is fighting, while a Veteran masses an army, counter-picks its production, and
comes home when its base is threatened.
"""

from __future__ import annotations

import random

from .fog import VisionCache, team_vision
from .grid import chebyshev, manhattan
from .units import BUILD_RADIUS, BUILDING, UNIT, UNIT_CAP

#: (army size before attacking, chance of counter-picking, defends base?)
SKILLS = {
    "novice": (2, 0.0, False),
    "moderate": (4, 0.4, True),
    "veteran": (6, 0.85, True),
    "cyborg": (7, 1.0, True),
}
SKILL_ORDER = ("novice", "moderate", "veteran", "cyborg")

#: Keep this much supply spare before committing to a Barracks, so a bot does
#: not bankrupt itself into having no army at all.
BARRACKS_BUFFER = 2

DEFEND_RADIUS = 7

#: How much stronger than the visible enemy force a bot wants to be before it
#: stops trading in the middle and marches on a Command Post.
PRESS_ADVANTAGE = 1.35

#: A threat must be at least this costly, and this large a share of our own
#: army, before it is worth pulling troops off an attack.
MIN_THREAT = 8
THREAT_SHARE = 0.30


class BotBrain:
    def __init__(self, skill: str = "moderate",
                 rng: random.Random | None = None) -> None:
        self.skill = skill if skill in SKILLS else "moderate"
        self.rng = rng or random.Random()

    # -- top level ---------------------------------------------------------
    def plan(self, match, me) -> list:
        state = match.state
        cache = VisionCache(state.map)
        vision = team_vision(state, me.team, cache)

        my_units = state.units_of(me.pid)
        my_buildings = [b for b in state.buildings_of(me.pid)]
        enemies = [u for u in state.units.values()
                   if u.alive and not state.allied(u.owner, me.pid)
                   and u.tile in vision]
        enemy_buildings = [b for b in state.buildings.values()
                           if b.alive and not state.allied(b.owner, me.pid)
                           and b.tile in vision]

        orders: list = []
        orders += self._economy(match, me, my_buildings, enemies)
        orders += self._army(match, me, my_units, my_buildings, enemies,
                             enemy_buildings, vision)
        return orders

    # -- production --------------------------------------------------------
    def _economy(self, match, me, my_buildings, enemies) -> list:
        state = match.state
        orders = []
        budget = me.supply
        bases = [b for b in my_buildings if b.code == "base" and b.operational]
        barracks = [b for b in my_buildings if b.code == "barracks"]
        if not bases:
            return orders

        # A Barracks is the gate to the whole counter triangle, so build one
        # as soon as it will not leave us defenceless.
        barracks_cost = BUILDING["barracks"].cost
        if not barracks and budget >= barracks_cost + BARRACKS_BUFFER:
            site = self._build_site(state, me, bases[0])
            if site is not None:
                orders.append({"o": "build", "bid": bases[0].bid,
                               "code": "barracks", "to": list(site)})
                budget -= barracks_cost

        room = UNIT_CAP - (len(state.units_of(me.pid))
                           + sum(len(b.queue) for b in my_buildings))
        wanted = self._next_unit(state, me, barracks, enemies)
        for _ in range(min(3, max(0, room))):
            if wanted is None:
                break
            unit_type = UNIT[wanted]
            if budget < unit_type.cost:
                break
            source = self._producer(my_buildings, wanted)
            if source is None:
                break
            orders.append({"o": "train", "bid": source.bid, "code": wanted})
            budget -= unit_type.cost
            wanted = self._next_unit(state, me, barracks, enemies)
        return orders

    def _producer(self, my_buildings, code: str):
        needed = UNIT[code].built_at
        options = [b for b in my_buildings
                   if b.code == needed and b.operational and len(b.queue) < 3]
        options.sort(key=lambda b: (len(b.queue), b.bid))
        return options[0] if options else None

    def _next_unit(self, state, me, barracks, enemies) -> str | None:
        """Pick the next thing to build, counter-picking when skilled enough."""
        _, counter_chance, _ = SKILLS[self.skill]
        have_barracks = any(b.operational for b in barracks)

        if have_barracks and enemies and self.rng.random() < counter_chance:
            # Answer whatever the enemy has most of.
            tally: dict[str, int] = {}
            for enemy in enemies:
                tally[enemy.code] = tally.get(enemy.code, 0) + 1
            common = max(tally, key=lambda c: (tally[c], c))
            for code, unit in UNIT.items():
                if unit.beats == common and (unit.built_at == "base" or have_barracks):
                    return code

        pool = ["trooper", "scout"]
        if have_barracks:
            pool = ["trooper", "ranged", "bruiser", "scout"]
            weights = [4, 3, 2, 1]
        else:
            weights = [4, 2]
        return self.rng.choices(pool, weights=weights, k=1)[0]

    def _build_site(self, state, me, base):
        """A free tile near the base, biased away from the map edge."""
        candidates = []
        for dy in range(-BUILD_RADIUS, BUILD_RADIUS + 1):
            for dx in range(-BUILD_RADIUS, BUILD_RADIUS + 1):
                tile = (base.x + dx, base.y + dy)
                if not state.map.passable(*tile) or state.map.is_node(*tile):
                    continue
                if chebyshev(tile, base.tile) < 2:
                    continue
                if tile in state.occupancy():
                    continue
                candidates.append(tile)
        if not candidates:
            return None
        candidates.sort(key=lambda t: (chebyshev(t, base.tile), t))
        return candidates[0]

    # -- army --------------------------------------------------------------
    def _army(self, match, me, my_units, my_buildings, enemies,
              enemy_buildings, vision) -> list:
        state = match.state
        mass_at, _, defends = SKILLS[self.skill]
        orders = []

        bases = [b for b in my_buildings if b.code == "base"]
        home = bases[0].tile if bases else None
        fighters = [u for u in my_units if u.code != "scout"]
        scouts = [u for u in my_units if u.code == "scout"]

        # 1. Home defence, but proportionate. Recalling the whole army every
        # time a lone scout wanders past the base makes two defensive bots
        # yo-yo forever and neither ever commits to a siege -- measured at 7
        # matches in 8 failing to resolve. So a token raid is ignored, and a
        # real threat pulls back only the half of the army nearest home.
        defenders: list = []
        threat_tile = None
        if defends and home is not None and fighters:
            near = [e for e in enemies if chebyshev(e.tile, home) <= DEFEND_RADIUS]
            threat_cost = sum(UNIT[e.code].cost for e in near if e.code in UNIT)
            army_cost = sum(UNIT[u.code].cost for u in fighters)
            if near and threat_cost >= max(MIN_THREAT, army_cost * THREAT_SHARE):
                threat_tile = min(near, key=lambda e: chebyshev(e.tile, home)).tile
                by_home = sorted(fighters, key=lambda u: chebyshev(u.tile, home))
                defenders = by_home[:max(1, len(by_home) // 2)]

        attackers = [u for u in fighters if u not in defenders]
        for unit in defenders:
            orders.append({"o": "attack", "uid": unit.uid, "to": list(threat_tile)})

        target = None
        if attackers:
            mine = sum(UNIT[u.code].cost for u in attackers)
            theirs = sum(UNIT[e.code].cost for e in enemies if e.code in UNIT)
            # Attack when ahead -- or when the army is as big as it will ever
            # get. At the cap, waiting buys nothing and hands the initiative
            # away, which is exactly why human RTS players push at max supply.
            at_cap = len(my_units) >= UNIT_CAP - 2
            pressing = (not enemies) or at_cap or mine >= theirs * PRESS_ADVANTAGE

            if len(attackers) >= mass_at or at_cap:
                if pressing:
                    # Finish somebody off rather than spreading damage around.
                    # In a four-way game especially, knocking one commander out
                    # shrinks the field and gets the match moving; picking the
                    # nearest target instead leaves everyone alive forever.
                    target = self._weakest_enemy_base(match, me, enemy_buildings,
                                                      home)
                elif enemies:
                    target = min(enemies, key=lambda e: manhattan(
                        e.tile, home or e.tile)).tile
                else:
                    target = self._enemy_spawn(match, me)

        if target is not None:
            for unit in attackers:
                orders.append({"o": "attack", "uid": unit.uid, "to": list(target)})
        else:
            # Not ready to commit: spread out and take the map instead, which
            # is what actually wins the match.
            claimed = set()
            for unit in attackers:
                node = self._claim_node(state, me, unit, claimed)
                if node is not None:
                    claimed.add(node)
                    orders.append({"o": "move", "uid": unit.uid, "to": list(node)})

        for scout in scouts:
            spot = self._scout_target(match, me, scout, vision)
            if spot is not None:
                orders.append({"o": "move", "uid": scout.uid, "to": list(spot)})
        return orders

    def _claim_node(self, state, me, unit, taken=()):
        """Nearest node we do not already own. Capture is a trip, not a post."""
        best = None
        for node in state.map.nodes:
            if node in taken:
                continue
            owner = state.node_owner.get(node)
            if owner is not None and state.allied(owner, me.pid):
                continue
            distance = manhattan(node, unit.tile)
            if best is None or distance < best[0]:
                best = (distance, node)
        return best[1] if best else None

    def _weakest_enemy_base(self, match, me, enemy_buildings, home):
        """The Command Post of whichever rival is closest to being finished."""
        state = match.state
        sieges = [b for b in enemy_buildings if b.code == "base"]
        if sieges:
            def weakness(building):
                owner = building.owner
                strength = sum(UNIT[u.code].cost for u in state.units_of(owner)
                               if u.code in UNIT)
                return (strength + building.hp,
                        manhattan(building.tile, home or building.tile))
            return min(sieges, key=weakness).tile
        return self._enemy_spawn(match, me)

    def _enemy_spawn(self, match, me):
        """Where the enemy started. Public knowledge -- it is on the map."""
        state = match.state
        others = [p for p in state.players.values()
                  if p.alive and not state.allied(p.pid, me.pid)]
        if not others:
            return None
        slots = sorted(state.map.spawns)
        for player in sorted(others, key=lambda p: p.pid):
            index = sorted(state.players).index(player.pid) + 1
            if index in state.map.spawns:
                return state.map.spawns[index]
        return state.map.spawns[slots[-1]]

    def _scout_target(self, match, me, scout, vision):
        """Send scouts at whatever we cannot currently see."""
        state = match.state
        free = [n for n in state.map.nodes
                if not state.allied(state.node_owner.get(n, -99), me.pid)
                and manhattan(n, scout.tile) <= 12]
        if free:
            return min(free, key=lambda n: manhattan(n, scout.tile))
        unseen = [n for n in state.map.nodes if n not in vision]
        if unseen:
            return min(unseen, key=lambda n: manhattan(n, scout.tile))
        spawn = self._enemy_spawn(match, me)
        if spawn is not None and spawn not in vision:
            return spawn
        for _ in range(8):
            tile = (self.rng.randrange(state.map.width),
                    self.rng.randrange(state.map.height))
            if state.map.passable(*tile) and tile not in vision:
                return tile
        return None
