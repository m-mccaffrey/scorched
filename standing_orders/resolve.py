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
from .grid import NEIGHBOURS, chebyshev, find_path
from .state import Building, MatchState, Unit
from .units import (BUILD_RADIUS, BUILDING, NODE_INCOME, UNIT, UNIT_CAP,
                    damage_between, damage_to_building)

#: Beats per turn. Twelve divides evenly by every unit speed, which keeps all
#: movement arithmetic in integers.
SUBTICKS = 12

#: Units fire on beats divisible by this: three volleys per fully engaged turn.
ATTACK_EVERY = 4

#: Movement points charged for one tile of open ground (mirrors grid.COST_OPEN).
POINTS_PER_TILE = 12


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

def path_toward(tilemap, start, goal, blocked) -> list:
    """Path to ``goal``, or failing that to the closest tile beside it.

    Attack-move at an enemy names a tile that is by definition occupied, so
    "get next to it" is what the player actually meant.
    """
    path = find_path(tilemap, start, goal, blocked)
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
        path = find_path(tilemap, start, option, blocked)
        if path:
            return path
    return []


def apply_orders(state: MatchState, orders: dict, result: TurnResult) -> dict:
    """Validate and commit one turn's orders. Returns per-player rejections.

    Supply is charged here, at commit time, so two orders in the same turn
    cannot spend the same credits twice.
    """
    rejected: dict[int, list] = {}

    def reject(pid: int, why: str) -> None:
        rejected.setdefault(pid, []).append(why)

    # Everything stands still unless told otherwise -- a unit given no order
    # this turn holds its ground rather than continuing last turn's march,
    # which keeps a turn's plan readable to the player who wrote it.
    for unit in state.units.values():
        unit.path = []
        unit.stance = "hold"
        unit.move_points = 0

    occupancy = state.occupancy()

    for pid, player_orders in sorted(orders.items()):
        player = state.players.get(pid)
        if player is None or not player.alive:
            continue
        for order in player_orders:
            try:
                _apply_one(state, player, order, occupancy, result)
            except OrderError as exc:
                reject(pid, str(exc))
    return rejected


def _apply_one(state: MatchState, player, order: dict, occupancy: dict,
               result: TurnResult) -> None:
    kind = str(order.get("o", ""))

    if kind in ("move", "attack"):
        unit = state.units.get(int(order.get("uid", -1)))
        if unit is None or unit.owner != player.pid or not unit.alive:
            raise OrderError("no such unit")
        goal = _tile(order.get("to"))
        if goal is None or not state.map.inside(*goal):
            raise OrderError("target off the map")
        blocked = set(occupancy) - {unit.tile}
        unit.stance = kind
        unit.path = path_toward(state.map, unit.tile, goal, blocked)
        if not unit.path and goal != unit.tile:
            raise OrderError(f"{unit.type.name} cannot reach that tile")

    elif kind == "hold":
        unit = state.units.get(int(order.get("uid", -1)))
        if unit is None or unit.owner != player.pid:
            raise OrderError("no such unit")
        unit.stance = "hold"
        unit.path = []

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
        if _committed_units(state, player.pid) >= UNIT_CAP:
            raise OrderError(f"army is at its cap of {UNIT_CAP}")
        player.supply -= unit_type.cost
        building.queue.append([code, unit_type.build_turns])

    elif kind == "build":
        building = state.buildings.get(int(order.get("bid", -1)))
        code = str(order.get("code", ""))
        target = _tile(order.get("to"))
        if building is None or building.owner != player.pid:
            raise OrderError("no such building")
        if code not in BUILDING or code == "base":
            raise OrderError("cannot build that")
        if target is None:
            raise OrderError("nowhere to build")
        _check_build_site(state, player, target, occupancy)
        building_type = BUILDING[code]
        if player.supply < building_type.cost:
            raise OrderError(f"not enough supply for a {building_type.name}")
        player.supply -= building_type.cost
        new_building = state.add_building(player.pid, code, target[0], target[1],
                                          under=building_type.build_turns)
        # It occupies its tile from the moment the foundations go down, so
        # nobody can walk through a half-built barracks.
        occupancy[target] = ("building", new_building.bid)
        result.add(0, "found", bid=new_building.bid, owner=player.pid,
                   code=code, at=list(target), under=building_type.build_turns)

    elif kind == "cancel":
        building = state.buildings.get(int(order.get("bid", -1)))
        if building is None or building.owner != player.pid or not building.queue:
            raise OrderError("nothing to cancel")
        code, _turns = building.queue.pop()
        player.supply += UNIT[code].cost

    else:
        raise OrderError(f"unknown order '{kind}'")


def _committed_units(state: MatchState, pid: int) -> int:
    """Living units plus everything already queued -- the cap counts both."""
    alive = len(state.units_of(pid))
    queued = sum(len(b.queue) for b in state.buildings.values()
                 if b.owner == pid and b.alive)
    return alive + queued


def _check_build_site(state: MatchState, player, target, occupancy) -> None:
    if not state.map.passable(*target):
        raise OrderError("cannot build on that terrain")
    if target in occupancy:
        raise OrderError("that tile is occupied")
    near = any(
        chebyshev(target, b.tile) <= BUILD_RADIUS
        for b in state.buildings.values()
        if b.owner == player.pid and b.alive
    )
    if not near:
        raise OrderError(f"must build within {BUILD_RADIUS} tiles of your buildings")


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

    def __init__(self, state: MatchState) -> None:
        self.state = state
        self.result = TurnResult()
        self.occupancy = state.occupancy()
        self._cache = VisionCache(state.map)
        self._teams = sorted({p.team for p in state.players.values()})

    def run(self, orders: dict) -> tuple[TurnResult, dict]:
        rejected = apply_orders(self.state, orders, self.result)
        self.occupancy = self.state.occupancy()
        self._snapshot_vision(0)
        for beat in range(1, SUBTICKS + 1):
            moved = self._move_beat(beat)
            if beat % ATTACK_EVERY == 0:
                self._combat_beat(beat)
            self._snapshot_vision(beat, reuse=not moved)
        self._end_of_turn()
        return self.result, rejected

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
            unit.move_points += unit.type.speed
            nxt = unit.path[0]
            cost = self.state.map.cost(*nxt)
            if unit.move_points < cost:
                continue
            if nxt in self.occupancy:
                # Someone got there first. Standing orders do not adapt: the
                # unit stops, and the player sees why on the replay.
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

    # -- combat ------------------------------------------------------------
    def _combat_beat(self, beat: int) -> None:
        for unit in self._order_of_action():
            if not unit.alive:
                continue
            target = self._find_target(unit)
            if target is None:
                continue
            if isinstance(target, Unit):
                dealt = damage_between(unit.code, target.code)
                target.hp -= dealt
                self.result.add(beat, "shoot", uid=unit.uid, at=list(unit.tile),
                                tgt=target.uid, kind="unit", dmg=dealt,
                                hp=max(0, target.hp), to=list(target.tile))
                if target.hp <= 0:
                    self._kill_unit(beat, target)
            else:
                dealt = damage_to_building(unit.code)
                target.hp -= dealt
                self.result.add(beat, "shoot", uid=unit.uid, at=list(unit.tile),
                                tgt=target.bid, kind="building", dmg=dealt,
                                hp=max(0, target.hp), to=list(target.tile))
                if target.hp <= 0:
                    self._kill_building(beat, target)

    def _find_target(self, unit: Unit):
        """Nearest enemy in reach; units before buildings, weakest first.

        Automatic and free -- no target micromanagement. The decisions in this
        game happen while writing orders, not while watching them run.
        """
        reach = unit.type.reach
        best_unit = None
        best_unit_key = None
        best_building = None
        best_building_key = None
        for other in self.state.units.values():
            if not other.alive or self.state.allied(other.owner, unit.owner):
                continue
            distance = chebyshev(unit.tile, other.tile)
            if distance > reach:
                continue
            key = (distance, other.hp, other.uid)
            if best_unit_key is None or key < best_unit_key:
                best_unit, best_unit_key = other, key
        if best_unit is not None:
            return best_unit
        for building in self.state.buildings.values():
            if not building.alive or self.state.allied(building.owner, unit.owner):
                continue
            distance = chebyshev(unit.tile, building.tile)
            if distance > reach:
                continue
            key = (distance, building.hp, building.bid)
            if best_building_key is None or key < best_building_key:
                best_building, best_building_key = building, key
        return best_building

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
    def _end_of_turn(self) -> None:
        beat = SUBTICKS
        for building in sorted(self.state.buildings.values(), key=lambda b: b.bid):
            if not building.alive:
                continue
            if building.building_turns > 0:
                building.building_turns -= 1
                if building.building_turns == 0:
                    self.result.add(beat, "ready", bid=building.bid,
                                    at=list(building.tile), code=building.code)
                continue
            if building.queue:
                entry = building.queue[0]
                entry[1] -= 1
                if entry[1] <= 0:
                    self._spawn(beat, building, entry[0])
        self._capture_nodes(beat)
        self._pay_income(beat)
        before = {p.pid for p in self.state.players.values() if p.alive}
        self.state.prune()
        after = {p.pid for p in self.state.players.values() if p.alive}
        for pid in sorted(before - after):
            self.result.eliminated.append(pid)
            self.result.add(beat, "eliminated", pid=pid)

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
            amount += NODE_INCOME * self.state.nodes_of(player.pid)
            player.supply += amount
            self.result.income[player.pid] = amount
            self.result.add(beat, "income", pid=player.pid, amount=amount,
                            total=player.supply)


def resolve_turn(state: MatchState, orders: dict) -> tuple[TurnResult, dict]:
    """Play one turn out. Mutates ``state`` and returns (timeline, rejections)."""
    return Resolver(state).run(orders)
