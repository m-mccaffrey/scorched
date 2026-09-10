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
from .units import (BUILDING, RESEARCH_BY_CODE, UNIT, attack_bonus,
                    available_research, build_turns_for, cost_of_building,
                    damage_between, damage_to_building, hp_bonus)

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
        unit.goal = goal
        unit.job = None
        unit.path = path_toward(state.map, unit.tile, goal, blocked)
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
        blocked = set(occupancy) - {unit.tile}
        path = path_toward(state.map, unit.tile, target, blocked)
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

    elif kind == "cancel":
        building = state.buildings.get(int(order.get("bid", -1)))
        if building is None or building.owner != player.pid or not building.queue:
            raise OrderError("nothing to cancel")
        code, _turns = building.queue.pop()
        player.supply += UNIT[code].cost

    else:
        raise OrderError(f"unknown order '{kind}'")


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

    def __init__(self, state: MatchState) -> None:
        self.state = state
        self.result = TurnResult()
        self.occupancy = state.occupancy()
        self._cache = VisionCache(state.map)
        self._teams = sorted({p.team for p in state.players.values()})

    def run(self, orders: dict) -> tuple[TurnResult, dict]:
        rejected = apply_orders(self.state, orders, self.result)
        self.occupancy = self.state.occupancy()
        for unit in self.state.units.values():
            unit.rerouted = False              # one detour per unit per turn
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
        blocked = set(self.occupancy) - {unit.tile}
        goal = unit.goal
        path = path_toward(self.state.map, unit.tile, goal, blocked)
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
            target = self._find_target_at(unit.tile, unit.type.reach, unit.owner)
            if target is None:
                continue
            bonus = self._attack_bonus(unit.owner)
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

    def _attack_bonus(self, pid: int) -> int:
        player = self.state.players.get(pid)
        return attack_bonus(player.research) if player else 0

    def _shoot(self, beat: int, shooter_id: int, origin, target,
               damage_of) -> None:
        dealt = max(1, damage_of(target))
        target.hp -= dealt
        is_unit = isinstance(target, Unit)
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
            if not other.alive or self.state.allied(other.owner, owner):
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
            if not building.alive or self.state.allied(building.owner, owner):
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
    def _end_of_turn(self) -> None:
        beat = SUBTICKS
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
        self._settle_eliminations(beat)

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


def resolve_turn(state: MatchState, orders: dict) -> tuple[TurnResult, dict]:
    """Play one turn out. Mutates ``state`` and returns (timeline, rejections)."""
    return Resolver(state).run(orders)
