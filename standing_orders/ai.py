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
from dataclasses import dataclass

from .fog import VisionCache, team_vision
from .grid import chebyshev, manhattan
from .units import (HARVEST_RADIUS, UNIT, available_research,
                    cost_of_building)

@dataclass(frozen=True)
class Skill:
    """How competent a bot is, across every part of the game.

    Difficulty used to vary only in how a bot fought. Once the economy
    arrived, that stopped meaning anything: every level ran the same build,
    so outcomes converged on a coin flip and a Veteran beat a Novice barely
    more than half the time. Skill now covers economy as well, which is where
    RTS matches are actually decided.
    """
    mass_at: int          # fighters gathered before committing to an attack
    counter_pick: float   # chance of answering what the enemy actually fields
    defends: bool         # comes home when the base is threatened
    workers: int          # Engineers it will put to work
    researches: bool      # spends surplus on upgrades
    barracks: int         # production lines it will run at once
    expands: bool         # builds forward depots to grow its cap


SKILLS = {
    "novice": Skill(2, 0.0, False, 1, False, 1, False),
    "moderate": Skill(4, 0.4, True, 2, True, 2, True),
    "veteran": Skill(6, 0.85, True, 3, True, 3, True),
    "cyborg": Skill(7, 1.0, True, 4, True, 3, True),
}
SKILL_ORDER = ("novice", "moderate", "veteran", "cyborg")

#: Keep this much supply spare before committing to a Barracks, so a bot does
#: not bankrupt itself into having no army at all.
BARRACKS_BUFFER = 2

#: Surplus that makes a bot want another production line. One Barracks tops
#: out at roughly a unit a turn, which is exactly the rate an army bleeds at
#: the front -- so a single line is a permanent stalemate however good the
#: economy behind it.
EXPAND_SURPLUS = 26

#: Only start a research project with this much supply to spare, so teching
#: never comes at the price of an army.
RESEARCH_BUFFER = 20

DEFEND_RADIUS = 7

#: How much stronger than the visible enemy force a bot wants to be before it
#: stops trading in the middle and marches on a Command Post.
PRESS_ADVANTAGE = 1.35

#: Fighters this close to the spearhead count as "with the army". Without a
#: rally step the bot feeds reinforcements to the front one at a time, where
#: they lose every fight three-to-one; a tighter army cap used to hide this by
#: keeping everyone bunched together.
RALLY_RADIUS = 5

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
        # Put Engineers on nodes they can already work *before* spending them
        # on construction. Doing it the other way round sends the whole labour
        # force off to build a distant depot while a node beside the Command
        # Post sits idle.
        worker_orders, busy = self._workers(match, me, my_units)
        orders += worker_orders
        orders += self._economy(match, me, my_buildings, my_units, enemies, busy)
        orders += self._army(match, me, my_units, my_buildings, enemies,
                             enemy_buildings, vision)
        return orders

    # -- engineers ---------------------------------------------------------
    def _workers(self, match, me, my_units) -> tuple:
        """Park idle Engineers on nodes a depot can actually reach.

        Returns the orders and the set of Engineers now spoken for, so the
        economy does not hand the same worker a building job as well.
        """
        state = match.state
        receivers = state.receivers_of(me.pid)
        orders: list = []
        busy: set = set()
        taken = {(u.x, u.y) for u in my_units if u.builder}
        for worker in [u for u in my_units if u.builder]:
            if worker.job is not None:
                busy.add(worker.uid)
                continue
            if state.map.is_node(worker.x, worker.y) and any(
                    chebyshev(worker.tile, r.tile) <= HARVEST_RADIUS
                    for r in receivers):
                busy.add(worker.uid)           # already earning; leave it be
                continue
            node = self._workable_node(state, me, worker, receivers, taken)
            if node is not None:
                taken.add(node)
                busy.add(worker.uid)
                orders.append({"o": "move", "uid": worker.uid, "to": list(node)})
        return orders, busy

    def _workable_node(self, state, me, worker, receivers, taken):
        best = None
        for node in state.map.nodes:
            if node in taken:
                continue
            owner = state.node_owner.get(node)
            if owner is not None and not state.allied(owner, me.pid):
                continue
            if not any(chebyshev(node, r.tile) <= HARVEST_RADIUS
                       for r in receivers):
                continue
            distance = manhattan(node, worker.tile)
            if best is None or distance < best[0]:
                best = (distance, node)
        return best[1] if best else None

    # -- production --------------------------------------------------------
    def _economy(self, match, me, my_buildings, my_units, enemies,
                 busy=frozenset()) -> list:
        state = match.state
        orders: list = []
        budget = me.supply
        bases = [b for b in my_buildings if b.code == "base" and b.operational]
        barracks = [b for b in my_buildings if b.code == "barracks"]
        depots = [b for b in my_buildings if b.code == "depot"]
        workers = [u for u in my_units if u.builder]
        receivers = state.receivers_of(me.pid)
        # An Engineer standing on a live node is earning its keep; pulling it
        # off to go and build something is how a bot starves itself.
        earning = {u.uid for u in workers
                   if state.map.is_node(u.x, u.y)
                   and any(chebyshev(u.tile, r.tile) <= HARVEST_RADIUS
                           for r in receivers)}
        idle_workers = [u for u in workers
                        if u.job is None and u.uid not in earning
                        and u.uid not in busy]
        if not bases:
            return orders

        # 1. A depot wherever we are working, or want to work, a node. Without
        #    one in range the Engineer standing on the node sends nothing.
        for worker in (list(idle_workers) if SKILLS[self.skill].expands else []):
            node = self._node_needing_depot(state, me, worker, depots + bases)
            price = cost_of_building("depot", me.research)
            if node is None or budget < price:
                continue
            site = self._site_near(state, node)
            if site is None:
                continue
            orders.append({"o": "build", "uid": worker.uid, "code": "depot",
                           "to": list(site)})
            idle_workers.remove(worker)
            budget -= price
            depots = depots + [None]           # counts toward the cap estimate
            break

        # 2. Barracks: the gate to the counter triangle -- but only once the
        #    economy is running. Opening with a Barracks instead of a depot
        #    leaves a bot on one supply a turn for the rest of the match.
        price = cost_of_building("barracks", me.research)
        if (not barracks and idle_workers and depots
                and budget >= price + BARRACKS_BUFFER):
            site = self._site_near(state, bases[0].tile, radius=4)
            if site is not None:
                worker = idle_workers.pop(0)
                orders.append({"o": "build", "uid": worker.uid,
                               "code": "barracks", "to": list(site)})
                budget -= price

        # 3. More production once the economy outruns one Barracks.
        price = cost_of_building("barracks", me.research)
        if (barracks and len(barracks) < SKILLS[self.skill].barracks
                and idle_workers and budget >= price + EXPAND_SURPLUS):
            site = self._site_near(state, bases[0].tile, radius=5)
            if site is not None:
                worker = idle_workers.pop(0)
                orders.append({"o": "build", "uid": worker.uid,
                               "code": "barracks", "to": list(site)})
                budget -= price

        # 4. Research, once there is money doing nothing useful.
        if (SKILLS[self.skill].researches and bases[0].project is None
                and budget >= RESEARCH_BUFFER):
            options = available_research(me.research)
            affordable = [r for r in options if r.cost <= budget - BARRACKS_BUFFER]
            if affordable:
                pick = self.rng.choice(affordable)
                orders.append({"o": "research", "bid": bases[0].bid,
                               "code": pick.code})
                budget -= pick.cost

        # 5. Recruit, respecting the cap the depots actually support.
        room = state.army_cap_of(me.pid) - state.army_size(me.pid)
        wanted = self._next_unit(state, me, barracks, workers, enemies)
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
            workers = workers + ([None] if wanted == "worker" else [])
            wanted = self._next_unit(state, me, barracks, workers, enemies)
        return orders

    def _node_needing_depot(self, state, me, worker, receivers):
        """A node worth putting a depot beside: ours, or free, and out of range."""
        best = None
        for node in state.map.nodes:
            owner = state.node_owner.get(node)
            if owner is not None and not state.allied(owner, me.pid):
                continue
            if any(r is not None and chebyshev(node, r.tile) <= HARVEST_RADIUS
                   for r in receivers):
                continue
            distance = manhattan(node, worker.tile)
            if best is None or distance < best[0]:
                best = (distance, node)
        return best[1] if best else None

    def _site_near(self, state, origin, radius: int = 3):
        """A free, buildable tile close to somewhere."""
        occupied = set(state.occupancy())
        candidates = []
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                tile = (origin[0] + dx, origin[1] + dy)
                if not state.map.passable(*tile) or state.map.is_node(*tile):
                    continue
                if tile in occupied or chebyshev(tile, origin) < 1:
                    continue
                candidates.append(tile)
        if not candidates:
            return None
        candidates.sort(key=lambda t: (chebyshev(t, origin), t))
        return candidates[0]

    def _producer(self, my_buildings, code: str):
        needed = UNIT[code].built_at
        options = [b for b in my_buildings
                   if b.code == needed and b.operational and len(b.queue) < 3]
        options.sort(key=lambda b: (len(b.queue), b.bid))
        return options[0] if options else None

    def _next_unit(self, state, me, barracks, workers, enemies) -> str | None:
        """Pick the next thing to build, counter-picking when skilled enough."""
        counter_chance = SKILLS[self.skill].counter_pick
        have_barracks = any(b is not None and b.operational for b in barracks)

        # Engineers first: no economy without them, and the army cap cannot
        # grow until somebody is free to raise a depot.
        wanted = SKILLS[self.skill].workers
        if len(state.receivers_of(me.pid)) > 2:
            wanted += 1
        if len(workers) < wanted:
            return "worker"

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

    # -- army --------------------------------------------------------------
    def _army(self, match, me, my_units, my_buildings, enemies,
              enemy_buildings, vision) -> list:
        state = match.state
        skill = SKILLS[self.skill]
        mass_at, defends = skill.mass_at, skill.defends
        orders = []

        bases = [b for b in my_buildings if b.code == "base"]
        home = bases[0].tile if bases else None
        fighters = [u for u in my_units if u.code not in ("scout", "worker")]
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
            # Attack when ahead, or when the army is as big as it will ever
            # get -- at the cap, waiting buys nothing and hands the initiative
            # away, which is why human RTS players push at max supply.
            #
            # Seeing no enemy is emphatically *not* evidence of advantage.
            # Treating it as one made the bot all-in on the enemy base every
            # single turn from behind fog, so it never expanded, never
            # out-economied anybody, and simply fed its army in forever.
            spare = state.army_cap_of(me.pid) - state.army_size(me.pid)
            at_cap = spare <= 2
            pressing = at_cap or (bool(enemies)
                                  and mine >= theirs * PRESS_ADVANTAGE)

            if pressing and (len(attackers) >= mass_at or at_cap):
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
            # Gather before committing. The spearhead -- whoever is closest to
            # the objective -- holds while the rest close up, and the whole
            # force moves off together once it is worth moving.
            anchor = min(attackers, key=lambda u: manhattan(u.tile, target))
            grouped = [u for u in attackers
                       if manhattan(u.tile, anchor.tile) <= RALLY_RADIUS]
            if len(grouped) >= mass_at or at_cap:
                for unit in attackers:
                    orders.append({"o": "attack", "uid": unit.uid,
                                   "to": list(target)})
            else:
                for unit in attackers:
                    orders.append({"o": "attack", "uid": unit.uid,
                                   "to": list(anchor.tile)})
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
