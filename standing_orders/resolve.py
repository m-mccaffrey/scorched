"""The turn resolver: orders in, a frame-stamped timeline out.

This is the centre of the game. Everyone plans in secret, then the server runs
the turn here, once, and ships the resulting event list to every client to
replay. Because the turn is simulated exactly once, on one machine, a Pi and a
desktop cannot disagree about what happened -- the same trick Scorched uses for
a shell, applied to a whole army.

A turn is divided into :data:`SUBTICKS` beats. Units accrue movement points
each beat and step when they can afford the next tile, so a fast unit visibly
outpaces a slow one instead of everything teleporting at once. Combat happens
every :data:`ATTACK_EVERY` beats, which gives three volleys to a unit that
stays engaged for a whole turn -- long enough that fights spill across turns
and reinforcements matter.

Nothing here is random. Target selection and movement priority are settled by
explicit tie-breaks, so a turn is a pure function of the state and the orders.
"""

from __future__ import annotations

from .fog import VisionCache, team_vision
from .grid import COST_OPEN, NEIGHBOURS, chebyshev, find_path
from .state import Building, MatchState, Unit
from .units import (AIRSTRIKE_COST, AIRSTRIKE_RADIUS, BUILDING,
                    MEDIC_HEAL_COST, OFFICER_AURA, OFFICER_AURA_RADIUS,
                    OFFICER_RANK, RANK_ATTACK, RANK_HP, RESEARCH_BY_CODE, UNIT,
                    airstrike_damage, attack_bonus, available_research,
                    build_turns_for, cost_of_building, damage_between,
                    damage_to_building, hp_bonus, promotion_cost, rank_name)

#: Beats per turn. Twelve divides evenly by every unit speed, which keeps all
#: movement arithmetic in integers.
SUBTICKS = 12

#: Units fire on beats divisible by this: three volleys per fully engaged turn.
ATTACK_EVERY = 4

#: Movement points a unit earns per beat, per point of speed.
#:
#: Marching earns full pace; advancing under attack orders earns seven
#: eighths of it. Picking your way forward ready to fight is slower than
#: simply going somewhere, which gives the two orders a real trade-off
#: instead of making attack-move a strict improvement on move.
#:
#: Deliberately a small edge. At three quarters the handicap dominated: armies
#: took so much longer to cross that evenly matched bots stopped resolving
#: at all.
MOVE_PACE = 8
ADVANCE_PACE = 7

#: A world is simulated whole, every turn, and that is affordable because the
#: cost of a turn tracks the number of *armies* and how far they are walking
#: rather than the acreage they are walking over.
#:
#: Simulating only the regions where something could happen was tried and
#: removed. On a 224x128 world with four sides it left 93% of regions live
#: anyway -- waking a region has to wake its neighbours, or a unit stops dead
#: at a boundary -- and it measured 3% *slower* for the bookkeeping, while
#: carrying a real risk that a bug in it silently freezes a siege. The quiet
#: parts of a world were already nearly free; what costs is pathfinding.

#: The beat an airstrike lands on: halfway through the turn, not at the start.
#:
#: Landing it at beat zero would just hit where everyone was standing when
#: they wrote their orders, which is no decision at all -- you would be aiming
#: at a photograph. Halfway through, you are aiming at where you think the
#: enemy will have got to, which is the same guess the rest of the game asks
#: you to make.
AIRSTRIKE_BEAT = SUBTICKS // 2

#: Movement points charged for one tile of open ground (mirrors grid.COST_OPEN).
POINTS_PER_TILE = COST_OPEN

assert MOVE_PACE * SUBTICKS == POINTS_PER_TILE, \
    "a unit at speed 1 must cover exactly one open tile a turn when marching"


class TurnResult:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.subticks: int = SUBTICKS
        self.income: dict[int, int] = {}
        #: team -> {beat: frozenset(tiles)}, used to build each player's
        #: fogged view of the turn without simulating anything twice.
        self.vision: dict[int, dict[int, frozenset]] = {}
        self.killed_units: list[int] = []
        self.killed_buildings: list[int] = []
        self.eliminated: list[int] = []

    def add(self, subtick: int, event: str, **fields) -> None:
        # Note the parameter is not called "kind": several events carry a
        # "kind" field of their own (unit vs building) and it would collide.
        record = {"s": subtick, "e": event}
        record.update(fields)
        self.events.append(record)

    def to_wire(self) -> dict:
        return {"events": self.events, "subticks": self.subticks}


class OrderError(Exception):
    """An order the rules refuse. Reported to its author, never fatal."""


# ---------------------------------------------------------------------------
# Order application
# ---------------------------------------------------------------------------

def static_obstacles(occupancy: dict) -> set:
    """Tiles worth planning around: structures, and nothing else.

    Units are deliberately *not* obstacles at planning time. A plan is written
    a whole turn before it runs, by which point everyone has moved, so routing
    around where your own army happens to be standing buys nothing and sends
    units on absurd detours around their own front line. Units still occupy
    tiles when the turn actually resolves; a unit that finds its next step
    taken simply routes around it then, when the obstruction is real.
    """
    return {tile for tile, (kind, _ident) in occupancy.items()
            if kind == "building"}


#: How much searching a unit may do to plan its next leg.
#:
#: A* explores roughly the square of the distance, so planning a whole march
#: across a world cost 9ms a call and was 80% of a turn. The first fix guessed
#: a waypoint thirty tiles along the straight line -- which works beautifully
#: until the map has an inland sea in the middle, where the guess lands in the
#: water, the search fails, and every unit falls back to the full-world path it
#: was supposed to avoid. On a 384x256 continent that put turns at 770ms.
#:
#: A budget does the same job without being able to guess wrong: search this
#: far and walk to whatever got closest. It costs a bounded amount on any
#: terrain, and a unit always makes progress toward where it was sent.
PLAN_BUDGET = 900


def path_toward(tilemap, start, goal, blocked, limit: int = PLAN_BUDGET) -> list:
    """Path to ``goal``, or failing that to the closest tile beside it.

    Attack-move at an enemy names a tile that is by definition occupied, so
    "get next to it" is what the player actually meant.

    Every search here is budgeted, and that matters more than it looks. A
    search that *fails* explores everything it can reach before admitting it,
    and this routine can try nine of them -- the goal and its eight
    neighbours. On a 98,000-tile continent an unreachable target therefore
    cost nine near-complete sweeps of the world, which is what put the worst
    turn at 1.9 seconds.
    """
    path = find_path(tilemap, start, goal, blocked, limit=limit)
    if path or start == goal:
        return path
    options = []
    for dx, dy in NEIGHBOURS:
        neighbour = (goal[0] + dx, goal[1] + dy)
        if neighbour == start:
            return []                    # already adjacent; stand still
        if tilemap.passable(*neighbour) and neighbour not in blocked:
            options.append(neighbour)
    options.sort(key=lambda t: (abs(t[0] - start[0]) + abs(t[1] - start[1]), t))
    for option in options:
        path = find_path(tilemap, start, option, blocked, limit=limit)
        if path:
            return path
    return []


def apply_orders(state: MatchState, orders: dict, result: TurnResult,
                 strikes: list | None = None) -> dict:
    """Validate and commit one turn's orders. Returns per-player rejections.

    Supply is charged here, at commit time, so two orders in the same turn
    cannot spend the same credits twice.

    Airstrikes are paid for here but land mid-turn, so they are appended to
    ``strikes`` for the resolver to drop at :data:`AIRSTRIKE_BEAT`.
    """
    rejected: dict[int, list] = {}
    if strikes is None:
        strikes = []

    def reject(pid: int, why: str) -> None:
        rejected.setdefault(pid, []).append(why)

    # Orders stand until they are changed. Wiping every path at the start of a
    # turn meant a scout told to cross the map stopped after one turn, which is
    # not what anybody means by "go there"; it also meant a unit never carried
    # movement progress between turns, so a speed-1 Bruiser could never enter a
    # forest at all -- 12 points a turn against a 24-point tile.
    #
    # Only units that receive a fresh order this turn are reset.
    reordered = set()
    for player_orders in orders.values():
        if not isinstance(player_orders, list):
            continue
        for order in player_orders:
            if isinstance(order, dict) and order.get("o") in ("move", "attack",
                                                              "hold"):
                try:
                    reordered.add(int(order.get("uid", -1)))
                except (TypeError, ValueError):
                    continue
    for uid in reordered:
        unit = state.units.get(uid)
        if unit is not None:
            unit.path = []
            unit.stance = "hold"
            unit.move_points = 0
            unit.goal = None
            unit.job = None

    occupancy = state.occupancy()
    # Every unit plans around the same set of tiles -- a unit's own tile holds
    # a unit, never a building, so subtracting it from the obstacle set was
    # always a no-op. That makes the whole turn's routing cacheable by
    # (start, goal), which matters because bots reissue the same march every
    # turn and A* is the single most expensive thing in a self-play match.
    blocked = static_obstacles(occupancy)
    routes: dict = {}

    for pid, player_orders in sorted(orders.items()):
        player = state.players.get(pid)
        if player is None or not player.alive:
            continue
        for order in player_orders:
            try:
                _apply_one(state, player, order, occupancy, result, strikes,
                           blocked, routes)
            except OrderError as exc:
                reject(pid, str(exc))
    return rejected


def _approach(tilemap, start, goal, blocked):
    """The tile to actually walk to: ``goal``, or the best tile beside it.

    Attack-move at an enemy names a tile that is by definition occupied, so
    "get next to it" is what the player meant. Resolving that *before*
    searching matters enormously: the alternative is to search for the goal,
    fail, and then search for each of eight neighbours in turn. On a continent
    that was six hundred failed searches a turn and 98% of the cost of a turn,
    because a failed search explores everything it can reach before admitting
    defeat.
    """
    if goal not in blocked and tilemap.passable(*goal):
        return goal
    options = []
    for dx, dy in NEIGHBOURS:
        beside = (goal[0] + dx, goal[1] + dy)
        if beside == start:
            return start                     # already there; stand still
        if tilemap.passable(*beside) and beside not in blocked:
            options.append(beside)
    if not options:
        return None
    return min(options, key=lambda t: (abs(t[0] - start[0])
                                       + abs(t[1] - start[1]), t))


def _route(tilemap, start, goal, blocked, routes: dict) -> list:
    """The next leg toward ``goal``, memoised for the turn.

    Paths are returned as copies, because a unit pops tiles off its own path as
    it walks. A distant goal is searched only as far as the budget allows and
    the unit walks to whatever came closest; it keeps the real goal and plans
    the next leg when it runs out of road. One search, always.
    """
    key = (start, goal)
    path = routes.get(key)
    if path is None:
        target = _approach(tilemap, start, goal, blocked)
        if target is None or target == start:
            path = []
        else:
            path = find_path(tilemap, start, target, blocked,
                             limit=PLAN_BUDGET, partial=True)
        routes[key] = path
    return list(path)


def _apply_one(state: MatchState, player, order: dict, occupancy: dict,
               result: TurnResult, strikes: list, blocked: set,
               routes: dict) -> None:
    kind = str(order.get("o", ""))

    if kind in ("move", "attack"):
        unit = state.units.get(int(order.get("uid", -1)))
        if unit is None or unit.owner != player.pid or not unit.alive:
            raise OrderError("no such unit")
        goal = _tile(order.get("to"))
        if goal is None or not state.map.inside(*goal):
            raise OrderError("target off the map")
        unit.stance = kind
        unit.goal = goal
        unit.job = None
        unit.path = _route(state.map, unit.tile, goal, blocked, routes)
        if not unit.path and goal != unit.tile:
            raise OrderError(f"{unit.type.name} cannot reach that tile")

    elif kind == "hold":
        unit = state.units.get(int(order.get("uid", -1)))
        if unit is None or unit.owner != player.pid:
            raise OrderError("no such unit")
        unit.stance = "hold"
        unit.path = []
        unit.goal = None
        unit.job = None

    elif kind == "train":
        building = state.buildings.get(int(order.get("bid", -1)))
        code = str(order.get("code", ""))
        if building is None or building.owner != player.pid:
            raise OrderError("no such building")
        if not building.operational:
            raise OrderError(f"{building.type.name} is still under construction")
        if code not in BUILDING[building.code].produces:
            raise OrderError(f"{building.type.name} cannot train that")
        unit_type = UNIT[code]
        if player.supply < unit_type.cost:
            raise OrderError(f"not enough supply for a {unit_type.name}")
        if len(building.queue) >= 5:
            raise OrderError("production queue is full")
        cap = state.army_cap_of(player.pid)
        if state.army_size(player.pid) >= cap:
            raise OrderError(f"army is at its cap of {cap} -- build a depot")
        player.supply -= unit_type.cost
        building.queue.append([code, unit_type.build_turns])

    elif kind == "build":
        unit = state.units.get(int(order.get("uid", -1)))
        code = str(order.get("code", ""))
        target = _tile(order.get("to"))
        if unit is None or unit.owner != player.pid or not unit.alive:
            raise OrderError("no such unit")
        if not unit.builder:
            raise OrderError(f"a {unit.type.name} cannot build")
        if code not in BUILDING or not BUILDING[code].buildable:
            raise OrderError("cannot build that")
        if target is None:
            raise OrderError("nowhere to build")
        _check_build_site(state, target, occupancy)
        price = cost_of_building(code, player.research)
        if player.supply < price:
            raise OrderError(f"not enough supply for a {BUILDING[code].name}")
        # Charged at commit so two orders cannot spend the same credits, and
        # refunded if the site is taken by the time the Engineer arrives.
        player.supply -= price
        unit.job = (code, target)
        unit.stance = "hold"
        unit.goal = target
        path = _route(state.map, unit.tile, target, blocked, routes)
        # Stop one tile short: the structure needs the site itself free.
        unit.path = path[:-1] if path else []

    elif kind == "research":
        building = state.buildings.get(int(order.get("bid", -1)))
        code = str(order.get("code", ""))
        if building is None or building.owner != player.pid:
            raise OrderError("no such building")
        if building.code != "base" or not building.operational:
            raise OrderError("research happens at the Command Post")
        if building.project:
            raise OrderError("already researching something")
        project = RESEARCH_BY_CODE.get(code)
        if project is None or project not in available_research(player.research):
            raise OrderError("that project is not available")
        if player.supply < project.cost:
            raise OrderError(f"not enough supply for {project.name}")
        player.supply -= project.cost
        building.project = [code, project.turns]

    elif kind == "promote":
        unit = state.units.get(int(order.get("uid", -1)))
        if unit is None or unit.owner != player.pid or not unit.alive:
            raise OrderError("no such unit")
        price = promotion_cost(unit.rank)
        if not price:
            raise OrderError(f"{unit.type.name} cannot rise any further")
        if not unit.blooded:
            raise OrderError(
                f"{unit.type.name} has not been in a fight since its last "
                "promotion")
        if player.supply < price:
            raise OrderError(f"not enough supply to promote ({price})")
        player.supply -= price
        unit.rank += 1
        # Each rank has to be earned again, so a unit that traded one shot
        # cannot be bought all the way to Lieutenant in a single turn.
        unit.blooded = False
        unit.max_hp += RANK_HP
        unit.hp = unit.max_hp          # a promotion is also a night off
        result.add(0, "promote", uid=unit.uid, rank=unit.rank,
                   at=list(unit.tile), name=rank_name(unit.rank))

    elif kind == "airstrike":
        building = state.buildings.get(int(order.get("bid", -1)))
        target = _tile(order.get("to"))
        if building is None or building.owner != player.pid:
            raise OrderError("no such building")
        if not BUILDING[building.code].airstrikes or not building.operational:
            raise OrderError("airstrikes are called from an Airfield")
        if target is None or not state.map.inside(*target):
            raise OrderError("target off the map")
        if any(strike[0] == building.bid for strike in strikes):
            raise OrderError(f"{building.type.name} has already flown today")
        if player.supply < AIRSTRIKE_COST:
            raise OrderError(f"an airstrike costs {AIRSTRIKE_COST} supply")
        player.supply -= AIRSTRIKE_COST
        strikes.append((building.bid, player.pid, target))

    elif kind in ("propose", "accept", "declare", "gift", "armistice"):
        _diplomacy(state, player, order, kind, result)

    elif kind == "cancel":
        building = state.buildings.get(int(order.get("bid", -1)))
        if building is None or building.owner != player.pid or not building.queue:
            raise OrderError("nothing to cancel")
        code, _turns = building.queue.pop()
        player.supply += UNIT[code].cost

    else:
        raise OrderError(f"unknown order '{kind}'")


#: Pacts a commander may offer.
PACTS = ("truce", "alliance")

#: Turns an agreement holds before anyone may tear it up.
#:
#: Without this, a pact is worth nothing: bots signed and broke 87 treaties a
#: match, flipping the instant the arithmetic tipped, and the log became a wall
#: of declarations nobody could read. A signature that binds for a while is
#: what makes signing a decision, and what makes a betrayal ten turns later
#: feel like a betrayal rather than a rounding error.
PACT_BINDING = 10


def _diplomacy(state: MatchState, player, order: dict, kind: str,
               result: TurnResult) -> None:
    """Offers, signatures, declarations and gifts.

    Diplomacy is public. Every commander sees who signed what and who is about
    to break it, because a secret alliance in a game played round one table is
    just a conversation, and a betrayal nobody can see coming is a feel-bad
    rather than a twist.
    """
    if kind == "armistice":
        want = bool(order.get("on", True))
        if want:
            state.armistice.add(player.pid)
        else:
            state.armistice.discard(player.pid)
        result.add(0, "armistice", pid=player.pid, on=want,
                   says=(f"{player.name} calls for an armistice" if want
                         else f"{player.name} withdraws the armistice call"))
        return

    other_pid = int(order.get("to", order.get("from", -1)))
    other = state.players.get(other_pid)
    if other is None or other_pid == player.pid or not other.alive:
        raise OrderError("no such commander")
    pair = state.pair(player.pid, other_pid)

    if kind == "propose":
        pact = str(order.get("pact", "truce"))
        if pact not in PACTS:
            raise OrderError("that is not something you can propose")
        if state.pact_between(*pair) == pact:
            raise OrderError(f"you already have {'an' if pact[0] == 'a' else 'a'} {pact}")
        state.offers[(player.pid, other_pid)] = pact
        result.add(0, "offer", pid=player.pid, to=other_pid, pact=pact,
                   says=f"{player.name} offers {other.name} a {pact}")

    elif kind == "accept":
        pact = state.offers.get((other_pid, player.pid))
        if pact is None:
            raise OrderError(f"{other.name} has not offered you anything")
        state.pacts[pair] = pact
        state.pact_since[pair] = state.turn
        state.offers.pop((other_pid, player.pid), None)
        state.offers.pop((player.pid, other_pid), None)
        state.breaking.discard(pair)
        result.add(0, "pact", a=pair[0], b=pair[1], pact=pact,
                   says=f"{player.name} and {other.name} sign {'an' if pact[0] == 'a' else 'a'} {pact}")

    elif kind == "declare":
        if state.pact_between(*pair) == "war":
            raise OrderError(f"you are already at war with {other.name}")
        held = state.turn - state.pact_since.get(pair, state.turn)
        if held < PACT_BINDING:
            raise OrderError(
                f"your agreement with {other.name} holds for "
                f"{PACT_BINDING - held} more turn"
                f"{'s' if PACT_BINDING - held != 1 else ''}")
        # Announced now, effective at the end of the turn. Nobody gets knifed
        # in the same breath they were offered peace; you get one turn to brace.
        state.breaking.add(pair)
        state.armistice.discard(player.pid)
        result.add(0, "declare", pid=player.pid, to=other_pid,
                   says=f"{player.name} declares war on {other.name}")

    elif kind == "gift":
        amount = max(0, int(order.get("supply", 0)))
        if amount <= 0:
            raise OrderError("nothing to give")
        if player.supply < amount:
            raise OrderError("you do not have that much supply")
        player.supply -= amount
        other.supply += amount
        result.add(0, "gift", pid=player.pid, to=other_pid, amount=amount,
                   says=f"{player.name} sends {other.name} {amount} supply")


def _check_build_site(state: MatchState, target, occupancy) -> None:
    """Anywhere an Engineer can walk.

    There is no build radius any more: the Engineer has to physically get
    there, which is limit enough, and it makes forward depots and cheeky
    proxy towers into real options.
    """
    if not state.map.passable(*target):
        raise OrderError("cannot build on that terrain")
    if state.map.is_node(*target):
        raise OrderError("cannot build on a resource node")
    if target in occupancy:
        raise OrderError("that tile is occupied")


def _tile(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        return (int(value[0]), int(value[1]))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class Resolver:
    """Plays one turn out beat by beat."""

    def __init__(self, state: MatchState, vision: bool = True) -> None:
        self.state = state
        self.result = TurnResult()
        self.occupancy = state.occupancy()
        self._cache = VisionCache(state.map)
        self._teams = sorted(state.blocs())
        #: Recording what each side saw at each beat is a third of the cost of
        #: a turn, and it exists only so the timeline can be cut down per
        #: player. A match nobody is watching -- a self-play game in a
        #: parameter search -- needs the outcome, not the presentation.
        self._vision = vision
        self._strikes: list = []
        #: Units under medical care this turn, settled before a shot is fired.
        self.patients: dict = {}

    def run(self, orders: dict) -> tuple[TurnResult, dict]:
        rejected = apply_orders(self.state, orders, self.result, self._strikes)
        self.occupancy = self.state.occupancy()
        for unit in self.state.units.values():
            unit.rerouted = False              # one detour per unit per turn
        self._admit_patients()
        self._snapshot_vision(0)
        for beat in range(1, SUBTICKS + 1):
            moved = self._move_beat(beat)
            if beat == AIRSTRIKE_BEAT:
                self._airstrikes(beat)
            if beat % ATTACK_EVERY == 0:
                self._combat_beat(beat)
            self._snapshot_vision(beat, reuse=not moved)
        self._end_of_turn()
        return self.result, rejected

    # -- medical -----------------------------------------------------------
    def _admit_patients(self) -> None:
        """Decide who is under care before the shooting starts.

        Care is opt-in: a unit is a patient only if it was told to hold, is
        hurt, and is already standing inside a friendly Field Hospital's
        radius. Marching past your own hospital must never quietly disarm a
        unit, and the whole point of the building is that the trade is a
        decision -- you give up a rifle for the turn to get the health back.

        Settled once, up front, because healing is paid out at the end of the
        turn and the no-shooting rule applies from the start of it.
        """
        for building in sorted(self.state.buildings.values(),
                               key=lambda b: b.bid):
            info = BUILDING[building.code]
            if not building.alive or not building.operational or not info.heal:
                continue
            for unit in sorted(self.state.units.values(), key=lambda u: u.uid):
                if not unit.alive or unit.uid in self.patients:
                    continue
                if not self.state.allied(unit.owner, building.owner):
                    continue
                if unit.stance != "hold" or unit.hp >= unit.max_hp:
                    continue
                if chebyshev(unit.tile, building.tile) <= info.heal_radius:
                    self.patients[unit.uid] = building.bid

    def _treat_patients(self, beat: int) -> None:
        """Pay for and apply a turn's healing.

        Metered, not free: every point costs supply, so a hospital is a place
        to spend a surplus rather than a one-off purchase that makes attrition
        stop mattering. A player who cannot pay simply gets less healing.
        """
        for uid, bid in sorted(self.patients.items()):
            unit = self.state.units.get(uid)
            building = self.state.buildings.get(bid)
            if unit is None or not unit.alive:
                continue
            if building is None or not building.alive or not building.operational:
                continue
            player = self.state.players.get(unit.owner)
            if player is None:
                continue
            wanted = min(BUILDING[building.code].heal, unit.max_hp - unit.hp)
            if wanted <= 0:
                continue
            afforded = min(wanted, player.supply // MEDIC_HEAL_COST)
            if afforded <= 0:
                continue
            player.supply -= afforded * MEDIC_HEAL_COST
            unit.hp += afforded
            self.result.add(beat, "heal", uid=unit.uid, bid=bid,
                            amount=afforded, hp=unit.hp, at=list(unit.tile))

    # -- air support -------------------------------------------------------
    def _airstrikes(self, beat: int) -> None:
        """Drop every strike bought this turn, in a fixed order.

        The blast does not care whose troops are underneath it. That is the
        whole balance of the thing: an airstrike is cheap enough to use often
        and indiscriminate enough that using it on a melee costs you as much
        as it costs them.
        """
        for bid, pid, target in sorted(self._strikes):
            airfield = self.state.buildings.get(bid)
            if airfield is None or not airfield.alive:
                continue                      # the field was bombed first
            self.result.add(beat, "strike", bid=bid, pid=pid, at=list(target),
                            radius=AIRSTRIKE_RADIUS)
            casualties = []
            for unit in sorted(self.state.units.values(), key=lambda u: u.uid):
                if not unit.alive:
                    continue
                damage = airstrike_damage(chebyshev(unit.tile, target))
                if damage:
                    casualties.append((unit, damage))
            for building in sorted(self.state.buildings.values(),
                                   key=lambda b: b.bid):
                if not building.alive:
                    continue
                damage = airstrike_damage(chebyshev(building.tile, target))
                if damage:
                    casualties.append((building, damage))
            for victim, damage in casualties:
                self._shoot(beat, -bid, target, victim, lambda _v, d=damage: d,
                            blood=False)

    # -- movement ----------------------------------------------------------
    def _order_of_action(self) -> list:
        """Faster units act first, so a Scout wins a race to a chokepoint.

        Ties break by owner and then unit id -- arbitrary but fixed, which is
        what matters: the same turn must always resolve the same way.
        """
        movers = [u for u in self.state.units.values() if u.alive]
        movers.sort(key=lambda u: (u.type.initiative, u.owner, u.uid))
        return movers

    def _snapshot_vision(self, beat: int, reuse: bool = False) -> None:
        """Record what each team can see at this beat.

        When nothing moved we reuse the previous beat's sets outright -- the
        discs are memoised anyway, but skipping the unions keeps the cost of a
        quiet turn near zero, which matters on a Pi.
        """
        if not self._vision:
            return
        for team in self._teams:
            per_beat = self.result.vision.setdefault(team, {})
            if reuse and beat > 0 and (beat - 1) in per_beat:
                per_beat[beat] = per_beat[beat - 1]
            else:
                per_beat[beat] = team_vision(self.state, team, self._cache)

    def _move_beat(self, beat: int) -> bool:
        moved = False
        for unit in self._order_of_action():
            if not unit.alive or not unit.path:
                continue
            if unit.stance == "attack" and self._find_target(unit) is not None:
                continue                     # engaged: stop and fight
            pace = ADVANCE_PACE if unit.stance == "attack" else MOVE_PACE
            unit.move_points += unit.type.speed * pace * self.state.map.info.pace
            nxt = unit.path[0]
            cost = self.state.map.cost(*nxt)
            if unit.move_points < cost:
                continue
            if nxt in self.occupancy:
                # Someone got there first. Try once to go around -- with
                # barricades, depots and towers on the board, a unit that
                # simply stopped dead at the first obstacle would be
                # unbearable. If there is genuinely no way through, halt and
                # say so on the replay.
                if self._reroute(unit):
                    continue
                unit.path = []
                unit.move_points = 0
                self.result.add(beat, "block", uid=unit.uid, at=list(nxt))
                continue
            unit.move_points -= cost
            del self.occupancy[unit.tile]
            unit.x, unit.y = nxt
            self.occupancy[unit.tile] = ("unit", unit.uid)
            unit.path.pop(0)
            moved = True
            self.result.add(beat, "move", uid=unit.uid, to=list(nxt))
        return moved

    def _reroute(self, unit: Unit) -> bool:
        """Recompute a path around whatever appeared in the way. Once a turn."""
        if unit.rerouted or unit.goal is None:
            return False
        unit.rerouted = True
        # A reroute happens mid-turn against a real obstruction, so here the
        # units standing in the way genuinely do count.
        blocked = set(self.occupancy) - {unit.tile}
        goal = unit.goal
        target = _approach(self.state.map, unit.tile, goal, blocked)
        path = [] if target in (None, unit.tile) else find_path(
            self.state.map, unit.tile, target, blocked,
            limit=PLAN_BUDGET, partial=True)
        if unit.job is not None and path:
            path = path[:-1]
        if not path:
            return False
        unit.path = path
        return True

    # -- combat ------------------------------------------------------------
    def _combat_beat(self, beat: int) -> None:
        for unit in self._order_of_action():
            if not unit.alive:
                continue
            if unit.uid in self.patients:
                continue                     # under care: no rifle this turn
            target = self._find_target_at(unit.tile, unit.type.reach, unit.owner)
            if target is None:
                continue
            bonus = self._unit_attack_bonus(unit)
            self._shoot(beat, unit.uid, unit.tile, target,
                        lambda tgt, code=unit.code, b=bonus: (
                            damage_between(code, tgt.code, b)
                            if isinstance(tgt, Unit)
                            else damage_to_building(code, tgt.code, b)))
        # Sentry towers fire too, and are the only structure that does.
        for tower in sorted(self.state.buildings.values(), key=lambda b: b.bid):
            info = BUILDING[tower.code]
            if not tower.operational or not info.attack:
                continue
            target = self._find_target_at(tower.tile, info.reach, tower.owner)
            if target is None:
                continue
            damage = info.attack + self._attack_bonus(tower.owner)
            self._shoot(beat, -tower.bid, tower.tile, target,
                        lambda _tgt, d=damage: d)
            # A tower cannot be promoted, but the poor soul it shot at has
            # certainly been in a fight.

    def _attack_bonus(self, pid: int) -> int:
        player = self.state.players.get(pid)
        return attack_bonus(player.research) if player else 0

    def _unit_attack_bonus(self, unit: Unit) -> int:
        """Research, plus the unit's own rank, plus any officer leading it.

        Only the nearest officer counts. Stacking auras would make a huddle of
        Lieutenants the whole game, and this way an officer is worth escorting
        rather than worth hoarding.
        """
        bonus = self._attack_bonus(unit.owner) + unit.rank * RANK_ATTACK
        if unit.rank < OFFICER_RANK and self._led_by_officer(unit):
            bonus += OFFICER_AURA
        return bonus

    def _led_by_officer(self, unit: Unit) -> bool:
        for other in self.state.units.values():
            if other.uid == unit.uid or not other.alive:
                continue
            if other.rank < OFFICER_RANK:
                continue
            if not self.state.allied(other.owner, unit.owner):
                continue
            if chebyshev(other.tile, unit.tile) <= OFFICER_AURA_RADIUS:
                return True
        return False

    def _shoot(self, beat: int, shooter_id: int, origin, target,
               damage_of, blood: bool = True) -> None:
        dealt = max(1, damage_of(target))
        target.hp -= dealt
        is_unit = isinstance(target, Unit)
        if blood:
            # Both parties have now been in a fight, which is what makes them
            # eligible for promotion. Being bombed does not count: a rank has
            # to be earned against somebody who was shooting back.
            shooter = self.state.units.get(shooter_id)
            if shooter is not None:
                shooter.blooded = True
            if is_unit:
                target.blooded = True
        self.result.add(beat, "shoot", uid=shooter_id, at=list(origin),
                        tgt=target.uid if is_unit else target.bid,
                        kind="unit" if is_unit else "building", dmg=dealt,
                        hp=max(0, target.hp), to=list(target.tile))
        if target.hp <= 0:
            if is_unit:
                self._kill_unit(beat, target)
            else:
                self._kill_building(beat, target)

    def _find_target_at(self, origin, reach: int, owner: int):
        """Nearest enemy in reach; units before buildings, weakest first.

        Automatic and free -- no target micromanagement. The decisions in this
        game happen while writing orders, not while watching them run.
        """
        best_unit = None
        best_unit_key = None
        best_building = None
        best_building_key = None
        for other in self.state.units.values():
            if not other.alive or not self.state.hostile(other.owner, owner):
                continue
            distance = chebyshev(origin, other.tile)
            if distance > reach:
                continue
            key = (distance, other.hp, other.uid)
            if best_unit_key is None or key < best_unit_key:
                best_unit, best_unit_key = other, key
        if best_unit is not None:
            return best_unit
        for building in self.state.buildings.values():
            if not building.alive or not self.state.hostile(building.owner, owner):
                continue
            distance = chebyshev(origin, building.tile)
            if distance > reach:
                continue
            key = (distance, building.hp, building.bid)
            if best_building_key is None or key < best_building_key:
                best_building, best_building_key = building, key
        return best_building

    def _find_target(self, unit: Unit):
        return self._find_target_at(unit.tile, unit.type.reach, unit.owner)

    def _kill_unit(self, beat: int, unit: Unit) -> None:
        unit.hp = 0
        self.occupancy.pop(unit.tile, None)
        self.result.killed_units.append(unit.uid)
        self.result.add(beat, "kill", uid=unit.uid, kind="unit",
                        at=list(unit.tile))

    def _kill_building(self, beat: int, building: Building) -> None:
        building.hp = 0
        self.occupancy.pop(building.tile, None)
        self.result.killed_buildings.append(building.bid)
        self.result.add(beat, "kill", uid=building.bid, kind="building",
                        at=list(building.tile))

    # -- end of turn -------------------------------------------------------
    def _march_on(self) -> None:
        """Give the next leg to anyone who walked to the end of their route.

        Units plan only as far as the horizon, so a long march is a series of
        legs rather than one enormous search. The unit has always kept its real
        goal; this is where it looks up and works out the next stretch.
        """
        blocked = static_obstacles(self.occupancy)
        routes: dict = {}
        for unit in sorted(self.state.units.values(), key=lambda u: u.uid):
            if not unit.alive or unit.path or unit.goal is None:
                continue
            if unit.tile == unit.goal or unit.job is not None:
                continue
            if unit.stance not in ("move", "attack"):
                continue
            path = _route(self.state.map, unit.tile, unit.goal, blocked, routes)
            if path:
                unit.path = path
            else:
                unit.goal = None          # nothing more to be done about it

    def _end_of_turn(self) -> None:
        beat = SUBTICKS
        self._march_on()
        self._work_sites(beat)
        for building in sorted(self.state.buildings.values(), key=lambda b: b.bid):
            if not building.alive:
                continue
            if building.building_turns > 0:
                builder = self._site_worker(building)
                if builder is None:
                    continue
                building.builder_uid = builder.uid
                building.building_turns -= 1
                if building.building_turns == 0:
                    builder.job = None
                    self.result.add(beat, "ready", bid=building.bid,
                                    at=list(building.tile), code=building.code)
                continue
            if building.queue:
                entry = building.queue[0]
                entry[1] -= 1
                if entry[1] <= 0:
                    self._spawn(beat, building, entry[0])
            if building.project:
                self._tick_research(beat, building)
        self._capture_nodes(beat)
        self._pay_income(beat)
        self._treat_patients(beat)
        self._break_pacts(beat)
        self._settle_eliminations(beat)

    def _break_pacts(self, beat: int) -> None:
        """Turn this turn's declarations into actual wars.

        Declared at the top of the turn, effective at the bottom of it: the
        turn you announce, the guns stay quiet, and from the next one they do
        not. That one turn of warning is the difference between a betrayal and
        a cheap shot.
        """
        for pair in sorted(self.state.breaking):
            self.state.pacts.pop(pair, None)
            self.state.pact_since.pop(pair, None)
            self.state.offers.pop(pair, None)
            self.state.offers.pop((pair[1], pair[0]), None)
            self.result.add(beat, "war", a=pair[0], b=pair[1])
        self.state.breaking.clear()

    def _settle_eliminations(self, beat: int) -> None:
        """Knock out anyone who has lost their Command Post, and their army.

        Leaving a defeated commander's units on the board is worse than it
        sounds: nobody ever files orders for them again, so they stand where
        their last order left them -- immortal obstacles that still shoot at
        passers-by and can never win. The command post going up takes the
        force with it.
        """
        before = {p.pid for p in self.state.players.values() if p.alive}
        self.state.prune()
        after = {p.pid for p in self.state.players.values() if p.alive}
        for pid in sorted(before - after):
            self.result.eliminated.append(pid)
            self.result.add(beat, "eliminated", pid=pid)
            for unit in [u for u in self.state.units.values()
                         if u.owner == pid and u.alive]:
                self._kill_unit(beat, unit)
            for building in [b for b in self.state.buildings.values()
                             if b.owner == pid and b.alive]:
                self._kill_building(beat, building)
        if before != after:
            self.state.prune()

    def _work_sites(self, beat: int) -> None:
        """Lay foundations for Engineers who have reached their sites."""
        for unit in sorted(self.state.units.values(), key=lambda u: u.uid):
            if not unit.alive or unit.job is None:
                continue
            code, target = unit.job
            if chebyshev(unit.tile, target) > 1:
                continue                       # still walking
            existing = self.occupancy.get(target)
            if existing is not None:
                if existing[0] == "building":
                    continue                   # already under way here
                # Somebody parked on the site while we walked over. Refund
                # rather than silently swallowing the supply.
                player = self.state.players.get(unit.owner)
                if player is not None:
                    player.supply += cost_of_building(code, player.research)
                unit.job = None
                self.result.add(beat, "nosite", uid=unit.uid, at=list(target))
                continue
            done = self.state.players[unit.owner].research \
                if unit.owner in self.state.players else set()
            turns = build_turns_for(code, done)
            building = self.state.add_building(unit.owner, code, target[0],
                                               target[1], under=turns)
            building.builder_uid = unit.uid
            self.occupancy[target] = ("building", building.bid)
            self.result.add(beat, "found", bid=building.bid, owner=unit.owner,
                            code=code, at=list(target), under=turns)

    def _site_worker(self, building: Building):
        """Any friendly Engineer standing next to an unfinished structure.

        Tying progress to the *specific* Engineer that started it meant a
        single new order to that unit orphaned the site permanently: the
        supply was spent, the tile stayed blocked, and the build sat one turn
        from done for the rest of the match with nothing able to adopt it. In
        practice you hit that constantly, because the Engineer stays selected
        after you place a structure. Anyone with a toolbox can finish the job.
        """
        original = self.state.units.get(building.builder_uid)
        candidates = []
        for unit in self.state.units.values():
            if not unit.alive or not unit.builder:
                continue
            if not self.state.allied(unit.owner, building.owner):
                continue
            if chebyshev(unit.tile, building.tile) <= 1:
                candidates.append(unit)
        if not candidates:
            return None
        if original in candidates:
            return original
        return min(candidates, key=lambda u: u.uid)

    def _tick_research(self, beat: int, building: Building) -> None:
        building.project[1] -= 1
        if building.project[1] > 0:
            return
        code = building.project[0]
        building.project = None
        player = self.state.players.get(building.owner)
        if player is None:
            return
        before = hp_bonus(player.research)
        player.research.add(code)
        gained = hp_bonus(player.research) - before
        if gained:
            # Armour applies at once, to the troops already in the field.
            for unit in self.state.units.values():
                if unit.alive and unit.owner == player.pid:
                    unit.max_hp += gained
                    unit.hp += gained
        self.result.add(beat, "researched", pid=player.pid, code=code)

    def _spawn(self, beat: int, building: Building, code: str) -> None:
        tile = self._free_tile_near(building.tile)
        if tile is None:
            return                           # blocked in; try again next turn
        building.queue.pop(0)
        unit = self.state.add_unit(building.owner, code, tile[0], tile[1])
        self.occupancy[tile] = ("unit", unit.uid)
        self.result.add(beat, "spawn", uid=unit.uid, owner=unit.owner,
                        code=code, at=list(tile), hp=unit.hp)

    def _free_tile_near(self, origin):
        """Nearest free tile to a building, searched in a fixed ring order."""
        for radius in (1, 2):
            candidates = []
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    tile = (origin[0] + dx, origin[1] + dy)
                    if self.state.map.passable(*tile) and tile not in self.occupancy:
                        candidates.append(tile)
            if candidates:
                candidates.sort()
                return candidates[0]
        return None

    def _capture_nodes(self, beat: int) -> None:
        """Whoever is standing on a node at the end of the turn owns it."""
        for node in self.state.map.nodes:
            holder = self.occupancy.get(node)
            if holder is None:
                continue
            kind, ident = holder
            owner = (self.state.units[ident].owner if kind == "unit"
                     else self.state.buildings[ident].owner)
            if self.state.node_owner.get(node) == owner:
                continue
            self.state.node_owner[node] = owner
            self.result.add(beat, "capture", pid=owner, at=list(node))

    def _pay_income(self, beat: int) -> None:
        for player in sorted(self.state.players.values(), key=lambda p: p.pid):
            if not player.alive:
                continue
            amount = sum(BUILDING[b.code].income
                         for b in self.state.buildings.values()
                         if b.owner == player.pid and b.operational)
            amount += self.state.harvest_income(player.pid)
            player.supply += amount
            self.result.income[player.pid] = amount
            self.result.add(beat, "income", pid=player.pid, amount=amount,
                            total=player.supply)


def resolve_turn(state: MatchState, orders: dict,
                 vision: bool = True) -> tuple[TurnResult, dict]:
    """Play one turn out. Mutates ``state`` and returns (timeline, rejections).

    Pass ``vision=False`` for a match with no audience: the rules play out
    identically, but nothing is recorded for the fog filter to read.
    """
    return Resolver(state, vision=vision).run(orders)
