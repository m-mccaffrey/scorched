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
from .units import (AIRSTRIKE_COST, AIRSTRIKE_RADIUS, ARMY_CAP_MAX, BUILDING,
                    HARVEST_RADIUS, UNIT, available_research,
                    cost_of_building, max_rank, promotion_cost)

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
    #: How much of the support game it plays. 0 none at all; 1 raises a Field
    #: Hospital, pulls its wounded back to it and promotes veterans; 2 also
    #: runs an Airfield and calls strikes. Graded rather than a flag because
    #: these are the most expensive things in the game, and a bot that buys
    #: them badly is worse off than one that never buys them.
    supports: int


SKILLS = {
    "novice": Skill(2, 0.0, False, 1, False, 1, False, 0),
    "moderate": Skill(4, 0.4, True, 2, True, 2, True, 1),
    "veteran": Skill(6, 0.85, True, 3, True, 3, True, 2),
    "cyborg": Skill(7, 1.0, True, 4, True, 3, True, 2),
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

#: Supply a bot keeps back before buying into the support game at all. These
#: are luxuries: an Airfield bought instead of an army loses the match before
#: it ever gets to fly.
SUPPORT_BUFFER = 40

#: Promotions a bot will buy in one turn, and the surplus it insists on
#: keeping while it does. Both exist to stop ranks crowding out the army.
PROMOTIONS_PER_TURN = 1
PROMOTE_BUFFER = 60

#: A unit this far below full health is worth walking back to a hospital.
#: Higher than it looks on purpose -- a unit that trudges home over a scratch
#: spends more turns off the line than the health is worth.
WOUNDED_SHARE = 0.5

#: Enemies that have to be within one blast for a strike to be worth 30
#: supply. Two is about break-even against what it costs to replace them.
STRIKE_WORTH = 2

#: Supply held back from a strike. Far shallower than the reserve the other
#: luxuries keep, because a strike is the only one that pays off this turn.
STRIKE_RESERVE = 10

#: How much stronger than the visible enemy force a bot wants to be before it
#: stops trading in the middle and marches on a Command Post.
PRESS_ADVANTAGE = 1.35

#: Fighters this close to the spearhead count as "with the army". Without a
#: rally step the bot feeds reinforcements to the front one at a time, where
#: they lose every fight three-to-one; a tighter army cap used to hide this by
#: keeping everyone bunched together.
RALLY_RADIUS = 5

#: How close to the objective, or to a visible enemy, before a bot stops
#: marching and starts advancing ready to fight.
#:
#: Advancing the whole way is a serious mistake in this game: reinforcements
#: appear at home, so anything that slows an attacker across the map hands the
#: advantage to the defender. Bots that advanced from their own doorstep
#: stopped resolving matches at all.
CONTACT_DISTANCE = 5

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
        busy = set(busy)
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

        def hands_for(price: int, pull_at: int):
            """An Engineer for a support building, idle or otherwise.

            Engineers park on resource nodes and stay there, so after the
            opening there is essentially never an idle one -- which is why
            bots measured zero Hospitals and zero Airfields built in a whole
            match while sitting on hundreds of spare supply. A rich bot can
            afford to take an Engineer off a node for two turns.

            Only the support rules use this. Letting the economy rules bid the
            same way was worse, not better: whichever rule ran first took the
            Engineer, and a bot that spent its last one on a third Barracks
            never built a Depot at all.
            """
            if idle_workers:
                return idle_workers[0]
            if budget < price + pull_at:
                return None
            # Never take the last Engineer off the last node -- somebody has
            # to stay and work, or the surplus that justified this dries up.
            free = [w for w in workers
                    if w is not None and w.job is None and w.uid not in busy]
            spare = [w for w in free if w.uid not in earning]
            if spare:
                return spare[0]
            earners = [w for w in free if w.uid in earning]
            return earners[0] if len(earners) > 1 else None

        def commit(worker, code: str, site) -> None:
            if worker in idle_workers:
                idle_workers.remove(worker)
            busy.add(worker.uid)
            orders.append({"o": "build", "uid": worker.uid, "code": code,
                           "to": list(site)})

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
            commit(worker, "depot", site)
            budget -= price
            depots = depots + [None]           # counts toward the cap estimate
            break

        # 1b. A depot purely for the ceiling. Node-side depots alone top a bot
        #     out around 24 army however rich it gets, which is how bots ended
        #     matches sitting on a thousand unspent supply. If we are capped,
        #     the ceiling can still rise, and there is money doing nothing,
        #     the answer is another depot -- anywhere safe will do, since this
        #     one is bought for its supply_cap and not its reach.
        if (SKILLS[self.skill].expands and idle_workers
                and state.army_size(me.pid) >= state.army_cap_of(me.pid)
                and state.army_cap_of(me.pid) < ARMY_CAP_MAX):
            price = cost_of_building("depot", me.research)
            if budget >= price + EXPAND_SURPLUS:
                site = self._site_near(state, bases[0].tile, radius=4)
                if site is not None:
                    commit(idle_workers[0], "depot", site)
                    budget -= price

        # 2. Barracks: the gate to the counter triangle -- but only once the
        #    economy is running. Opening with a Barracks instead of a depot
        #    leaves a bot on one supply a turn for the rest of the match.
        price = cost_of_building("barracks", me.research)
        if (not barracks and idle_workers and depots
                and budget >= price + BARRACKS_BUFFER):
            site = self._site_near(state, bases[0].tile, radius=4)
            if site is not None:
                commit(idle_workers[0], "barracks", site)
                budget -= price

        # 3. More production once the economy outruns one Barracks.
        price = cost_of_building("barracks", me.research)
        if (barracks and len(barracks) < SKILLS[self.skill].barracks
                and idle_workers and budget >= price + EXPAND_SURPLUS):
            site = self._site_near(state, bases[0].tile, radius=5)
            if site is not None:
                commit(idle_workers[0], "barracks", site)
                budget -= price

        # 3b. Support buildings, once there is an army worth supporting. A
        #     Field Hospital first -- it pays back every turn there is a
        #     casualty -- and an Airfield only for bots that will actually fly
        #     it. Both are luxuries, so both wait behind a healthy buffer.
        tier = SKILLS[self.skill].supports
        for code, needed in (("medic", 1), ("airfield", 2)):
            # Gated on an economy, not on a Barracks. Requiring one meant
            # these never got built at all, because bots turn out to build a
            # Barracks far more rarely than they should -- a separate problem,
            # and not one a Field Hospital has any reason to wait behind.
            if tier < needed or not depots:
                continue
            if any(b.code == code for b in my_buildings):
                continue
            price = cost_of_building(code, me.research)
            # An Airfield has to be bought with its first sortie, or a bot
            # spends 14 supply and three Engineer-turns on a hangar it cannot
            # afford to use -- which measured as a straight loss: bots that
            # built one went from beating Moderate to losing to it 1-5.
            price += AIRSTRIKE_COST if code == "airfield" else 0
            if budget < price + SUPPORT_BUFFER:
                continue
            worker = hands_for(price, SUPPORT_BUFFER)
            site = self._site_near(state, bases[0].tile, radius=3)
            if worker is None or site is None:
                continue
            commit(worker, code, site)
            budget -= cost_of_building(code, me.research)

        # 3c. Airstrikes, before promotions and out of the same purse. These
        #     used to be planned in the army step against a second, private
        #     copy of the budget, so a bot happily promised the same supply to
        #     a strike and a promotion and had one of them thrown out.
        strike_orders, budget = self._airstrikes(state, me, my_buildings,
                                                 enemies, budget)
        orders += strike_orders

        # 3d. Promote whoever has earned it -- but only once quantity has run
        #     out. Promotions are cheap enough to be tempting every turn, and
        #     a bot that took them early spent its whole economy on ranks and
        #     never built a Barracks at all: 175 promotions in a match and no
        #     production. Buying quality is what you do when you cannot buy
        #     any more quantity.
        spare = state.army_cap_of(me.pid) - state.army_size(me.pid)
        if tier >= 1 and spare <= 2:
            veterans = sorted((u for u in my_units
                               if u.blooded and u.rank < max_rank()
                               and not u.builder),
                              key=lambda u: (-u.rank, u.uid))
            # One a turn, behind a deep buffer. Promotions are cheap enough
            # that a bot allowed to buy them freely will empty its treasury
            # into ranks the same turn it hits an early, tiny cap -- and an
            # early cap wants another Depot, not a Corporal.
            for unit in veterans[:PROMOTIONS_PER_TURN]:
                price = promotion_cost(unit.rank)
                if not price or budget < price + PROMOTE_BUFFER:
                    continue
                orders.append({"o": "promote", "uid": unit.uid})
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

        # 0. Casualties go to the rear. A hospital nobody walks to is 10
        # supply spent on scenery, and a badly hurt unit sent back into the
        # line is a free kill for the other side. They are taken off the
        # roster entirely for the trip -- a stretcher case is not an attacker.
        wards = [b for b in my_buildings
                 if BUILDING[b.code].heal and b.operational]
        if wards and SKILLS[self.skill].supports >= 1:
            for unit in list(fighters):
                if unit.hp >= unit.max_hp * WOUNDED_SHARE:
                    continue
                ward = min(wards, key=lambda b: manhattan(unit.tile, b.tile))
                bed = self._bedside(state, ward, unit)
                if bed is None:
                    continue
                fighters.remove(unit)
                if unit.tile == bed:
                    orders.append({"o": "hold", "uid": unit.uid})
                else:
                    orders.append({"o": "move", "uid": unit.uid,
                                   "to": list(bed)})

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
                    orders.append({"o": self._pace(unit, target, enemies),
                                   "uid": unit.uid, "to": list(target)})
            else:
                # Gathering happens behind your own lines: march, do not
                # advance. Rallying at cautious pace is pure lost time.
                for unit in attackers:
                    stance = "attack" if enemies else "move"
                    orders.append({"o": stance, "uid": unit.uid,
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

    def _bedside(self, state, ward, unit):
        """A free tile inside a hospital's radius, or the one already held."""
        info = BUILDING[ward.code]
        occupied = set(state.occupancy())
        best = None
        for dy in range(-info.heal_radius, info.heal_radius + 1):
            for dx in range(-info.heal_radius, info.heal_radius + 1):
                tile = (ward.x + dx, ward.y + dy)
                if tile == unit.tile:
                    return tile                # already in a bed
                if not state.map.passable(*tile) or tile in occupied:
                    continue
                distance = manhattan(tile, unit.tile)
                if best is None or distance < best[0]:
                    best = (distance, tile)
        return best[1] if best else None

    def _airstrikes(self, state, me, my_buildings, enemies,
                    budget: int) -> tuple:
        """Spend a strike on the densest thing worth bombing.

        Scored by what is actually under the blast, friendly casualties
        subtracted -- a bot that bombs its own melee is worse than one that
        never calls a strike at all.
        """
        if SKILLS[self.skill].supports < 2 or not enemies:
            return [], budget
        fields = [b for b in my_buildings
                  if BUILDING[b.code].airstrikes and b.operational]
        orders: list = []
        aimed: set = set()
        friends = [u for u in state.units.values()
                   if u.alive and state.allied(u.owner, me.pid)]
        for field in sorted(fields, key=lambda b: b.bid):
            # A strike is the one thing here that pays off the same turn, so
            # it is funded ahead of promotions and kept behind only a shallow
            # reserve -- a hangar that never launches is pure overhead.
            if budget < AIRSTRIKE_COST + STRIKE_RESERVE:
                break
            best = None
            for candidate in {e.tile for e in enemies} - aimed:
                caught = sum(1 for e in enemies
                             if chebyshev(e.tile, candidate) <= AIRSTRIKE_RADIUS)
                friendly = sum(1 for u in friends
                               if chebyshev(u.tile, candidate) <= AIRSTRIKE_RADIUS)
                score = caught - friendly
                if score < STRIKE_WORTH:
                    continue
                if best is None or (score, candidate) > best:
                    best = (score, candidate)
            if best is None:
                break
            aimed.add(best[1])
            orders.append({"o": "airstrike", "bid": field.bid,
                           "to": list(best[1])})
            budget -= AIRSTRIKE_COST
        return orders, budget

    def _pace(self, unit, target, enemies) -> str:
        """March or advance?

        Attack orders trade pace for stopping to fight whatever you meet.
        Worth paying at the point of contact; a waste while crossing empty
        ground, where arriving late is the only real danger. Units keep
        shooting either way -- the stance only decides whether they halt.
        """
        if manhattan(unit.tile, target) <= CONTACT_DISTANCE:
            return "attack"
        if any(manhattan(unit.tile, e.tile) <= CONTACT_DISTANCE for e in enemies):
            return "attack"
        return "move"

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
