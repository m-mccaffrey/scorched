"""Live match state: who owns what, standing where, with how much health."""

from __future__ import annotations

from dataclasses import dataclass, field

from .grid import TileMap, chebyshev
from .units import (BUILDING, HARVEST_RADIUS, UNIT, army_cap, harvest_rate,
                    hp_bonus)

MAX_PLAYERS = 4


@dataclass
class Unit:
    uid: int
    owner: int
    code: str
    x: int
    y: int
    hp: int
    #: Health ceiling, which Armour research raises above the type's base.
    max_hp: int = 0
    #: Tiles still to walk, nearest first. Recomputed whenever orders change.
    path: list = field(default_factory=list)
    #: "move" | "attack" | "hold"
    stance: str = "hold"
    move_points: int = 0
    #: Where the current march ends, so a blocked unit can route around
    #: rather than simply giving up.
    goal: tuple | None = None
    rerouted: bool = False
    #: An Engineer's outstanding construction job: (building code, tile).
    job: tuple | None = None
    #: Promotions earned, 0 through ``units.max_rank()``.
    rank: int = 0
    #: Has this unit fought since its last promotion? Supply alone does not
    #: make a veteran -- a unit has to have been in a fight to be eligible.
    blooded: bool = False

    def __post_init__(self) -> None:
        if not self.max_hp:
            self.max_hp = UNIT[self.code].hp

    @property
    def type(self):
        return UNIT[self.code]

    @property
    def tile(self) -> tuple[int, int]:
        return (self.x, self.y)

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def builder(self) -> bool:
        return UNIT[self.code].builder

    def to_wire(self) -> dict:
        return {"uid": self.uid, "owner": self.owner, "code": self.code,
                "x": self.x, "y": self.y, "hp": self.hp, "max": self.max_hp,
                "stance": self.stance, "path": [list(t) for t in self.path],
                "rank": self.rank, "blooded": self.blooded,
                "job": list(self.job[1]) + [self.job[0]] if self.job else None}


@dataclass
class Building:
    bid: int
    owner: int
    code: str
    x: int
    y: int
    hp: int
    #: Turns remaining until construction finishes; 0 means it is operational.
    building_turns: int = 0
    #: Production queue of (unit code, turns remaining).
    queue: list = field(default_factory=list)
    #: The Engineer raising this structure; progress needs them alongside.
    builder_uid: int = 0
    #: Command Posts only: the current project, as [code, turns remaining].
    project: list | None = None

    @property
    def type(self):
        return BUILDING[self.code]

    @property
    def tile(self) -> tuple[int, int]:
        return (self.x, self.y)

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def operational(self) -> bool:
        return self.hp > 0 and self.building_turns <= 0

    @property
    def wall(self) -> bool:
        return BUILDING[self.code].wall

    def to_wire(self) -> dict:
        return {"bid": self.bid, "owner": self.owner, "code": self.code,
                "x": self.x, "y": self.y, "hp": self.hp,
                "under": self.building_turns,
                "queue": [[c, t] for c, t in self.queue],
                "project": list(self.project) if self.project else None}


@dataclass
class Player:
    pid: int
    name: str
    team: int = 0
    color: int = 0
    supply: int = 0
    bot: bool = False
    skill: str = "moderate"
    connected: bool = True
    ready: bool = False
    alive: bool = True
    #: Completed research projects, applying to the whole force.
    research: set = field(default_factory=set)

    def to_wire(self, full: bool = False) -> dict:
        data = {"pid": self.pid, "name": self.name, "team": self.team,
                "color": self.color, "bot": self.bot, "alive": self.alive,
                "ready": self.ready, "connected": self.connected,
                "research": sorted(self.research)}
        if full:
            data["supply"] = self.supply
        return data


class MatchState:
    """Everything about a match in progress. The server owns the only copy."""

    def __init__(self, tilemap: TileMap) -> None:
        self.map = tilemap
        self.players: dict[int, Player] = {}
        self.units: dict[int, Unit] = {}
        self.buildings: dict[int, Building] = {}
        self.turn: int = 0
        #: Resource node tile -> owning pid. Capture persists: you take a node
        #: by standing on it once, and it keeps paying after you march on.
        #: Requiring a permanent garrison instead tied up whole armies and
        #: starved everyone, which made matches unwinnable grinds.
        self.node_owner: dict = {}
        self._next_uid = 1
        self._next_bid = 1
        #: Standing agreements, keyed by sorted pid pair. Absent means war.
        self.pacts: dict = {}
        #: The turn each agreement was signed, so one cannot be torn up the
        #: moment it stops being convenient.
        self.pact_since: dict = {}
        #: Offers on the table: (from, to) -> pact being offered.
        self.offers: dict = {}
        #: Commanders calling for the war to end, everyone keeping what they
        #: hold. It takes all of them.
        self.armistice: set = set()
        #: Declarations that take effect at the end of this turn. Breaking a
        #: pact is announced rather than instant, so nobody is knifed in the
        #: same breath they were offered peace -- you get one turn to brace.
        self.breaking: set = set()

    # -- spawning ----------------------------------------------------------
    def add_unit(self, owner: int, code: str, x: int, y: int) -> Unit:
        # New recruits arrive with whatever Armour research has been finished.
        done = self.players[owner].research if owner in self.players else set()
        ceiling = UNIT[code].hp + hp_bonus(done)
        unit = Unit(uid=self._next_uid, owner=owner, code=code, x=x, y=y,
                    hp=ceiling, max_hp=ceiling)
        self._next_uid += 1
        self.units[unit.uid] = unit
        return unit

    def add_building(self, owner: int, code: str, x: int, y: int,
                     under: int = 0) -> Building:
        building = Building(bid=self._next_bid, owner=owner, code=code, x=x,
                            y=y, hp=BUILDING[code].hp, building_turns=under)
        self._next_bid += 1
        self.buildings[building.bid] = building
        return building

    # -- queries -----------------------------------------------------------
    def occupancy(self) -> dict:
        """Tile -> ('unit', uid) or ('building', bid). One occupant per tile."""
        out = {}
        for building in self.buildings.values():
            if building.alive:
                out[building.tile] = ("building", building.bid)
        for unit in self.units.values():
            if unit.alive:
                out[unit.tile] = ("unit", unit.uid)
        return out

    def units_of(self, pid: int) -> list:
        return [u for u in self.units.values() if u.owner == pid and u.alive]

    def buildings_of(self, pid: int) -> list:
        return [b for b in self.buildings.values() if b.owner == pid and b.alive]

    def team_of(self, pid: int) -> int:
        player = self.players.get(pid)
        return player.team if player else -1

    # -- diplomacy ---------------------------------------------------------
    #
    # Two commanders are at war unless they have agreed otherwise. A truce
    # means neither side shoots; an alliance means they also share what they
    # can see and can work on each other's buildings. Everything downstream
    # asks these three questions rather than comparing team numbers, which is
    # what lets a war change shape halfway through.

    @staticmethod
    def pair(a: int, b: int) -> tuple:
        return (a, b) if a <= b else (b, a)

    def pact_between(self, a: int, b: int) -> str:
        if a == b:
            return "alliance"
        return self.pacts.get(self.pair(a, b), "war")

    def allied(self, a: int, b: int) -> bool:
        """Same side: shared sight, shared work, no friendly fire."""
        return a == b or self.pact_between(a, b) == "alliance"

    def hostile(self, a: int, b: int) -> bool:
        """Will shoot on sight. A truce stops this without making friends."""
        return a != b and self.pact_between(a, b) == "war"

    def at_peace(self, a: int, b: int) -> bool:
        return not self.hostile(a, b)

    def bloc_of(self, pid: int) -> int:
        """Which sight-sharing group a commander belongs to.

        Alliances are not transitive by agreement -- A and C may both be
        allied to B without having agreed anything with each other -- but
        vision has to be *some* partition, and anything else means sharing
        sight with someone you never signed with. Blocs are the connected
        components of the alliance graph, named by their lowest member, which
        keeps the answer stable and free of who-asked-first ordering.
        """
        seen = {pid}
        frontier = [pid]
        while frontier:
            current = frontier.pop()
            for other in self.players:
                if other in seen or not self.allied(current, other):
                    continue
                seen.add(other)
                frontier.append(other)
        return min(seen)

    def blocs(self) -> set:
        return {self.bloc_of(pid) for pid in self.players}

    def holding(self, pid: int) -> int:
        """What a commander would keep if the war stopped now.

        Resource nodes are the territory of this game -- the thing worth
        marching to and the thing worth arguing over -- so they carry the
        weight, with standing structures as the tiebreak. This is what makes a
        negotiated peace a *settlement* rather than an off-switch: without a
        result, stopping is free, every bot works that out, and a four-way war
        ends in handshakes on turn twenty-three with nobody having played.
        """
        nodes = sum(1 for owner in self.node_owner.values() if owner == pid)
        works = sum(1 for b in self.buildings.values()
                    if b.owner == pid and b.alive and b.operational)
        return nodes * 10 + works

    def bloc_holding(self, pid: int) -> int:
        """What a commander's whole side holds."""
        bloc = self.bloc_of(pid)
        return sum(self.holding(p.pid) for p in self.players.values()
                   if p.alive and self.bloc_of(p.pid) == bloc)

    def everyone_at_peace(self) -> bool:
        """Is anybody still shooting at anybody?"""
        living = [p.pid for p in self.players.values() if p.alive]
        return all(self.at_peace(a, b)
                   for index, a in enumerate(living) for b in living[index + 1:])

    def armistice_agreed(self) -> bool:
        """Has every commander still standing called for the war to end?

        Deliberately *not* the same question as "is anybody shooting". Two
        neighbours agreeing to stop fighting each other is a truce, and a
        four-way war is not over because two of its corners went quiet --
        which is exactly the mistake that had bots shaking hands on turn
        twenty with nobody having played. Ending the war is its own act, and
        it takes everybody.
        """
        living = [p.pid for p in self.players.values() if p.alive]
        return len(living) > 1 and all(pid in self.armistice for pid in living)

    def living_teams(self) -> set:
        """Blocs that still hold at least one Command Post."""
        teams = set()
        for building in self.buildings.values():
            if building.alive and building.code == "base":
                teams.add(self.bloc_of(building.owner))
        return teams

    def has_base(self, pid: int) -> bool:
        return any(b.alive and b.code == "base" and b.owner == pid
                   for b in self.buildings.values())

    def owned_building_codes(self, pid: int) -> set:
        return {b.code for b in self.buildings.values()
                if b.owner == pid and b.operational}

    # -- economy -----------------------------------------------------------
    def army_cap_of(self, pid: int) -> int:
        """How large an army this player's standing structures support."""
        return army_cap([b.code for b in self.buildings.values()
                         if b.owner == pid and b.operational])

    def army_size(self, pid: int) -> int:
        """Living units plus everything queued -- the cap counts both."""
        alive = len(self.units_of(pid))
        queued = sum(len(b.queue) for b in self.buildings.values()
                     if b.owner == pid and b.alive)
        return alive + queued

    def site_is_stalled(self, building) -> bool:
        """An unfinished structure with no friendly Engineer beside it."""
        if building.building_turns <= 0:
            return False
        from .grid import chebyshev
        return not any(
            u.alive and u.builder and self.allied(u.owner, building.owner)
            and chebyshev(u.tile, building.tile) <= 1
            for u in self.units.values())

    def receivers_of(self, pid: int) -> list:
        """Structures able to receive a worker's transmissions."""
        return [b for b in self.buildings.values()
                if b.owner == pid and b.operational and BUILDING[b.code].harvests]

    def harvest_income(self, pid: int) -> int:
        """Supply sent this turn by Engineers working nodes in depot range.

        Three things must line up: a worker standing on the node, a depot or
        Command Post within range, and that structure finished. Each of them
        is a thing an opponent can take away.
        """
        receivers = self.receivers_of(pid)
        if not receivers:
            return 0
        rate = harvest_rate(self.players[pid].research if pid in self.players
                            else set())
        total = 0
        for unit in self.units.values():
            if not unit.alive or unit.owner != pid or not unit.builder:
                continue
            if not self.map.is_node(unit.x, unit.y):
                continue
            if any(chebyshev(unit.tile, r.tile) <= HARVEST_RADIUS
                   for r in receivers):
                total += rate
        return total

    def prune(self) -> None:
        """Drop the dead, and eliminate players who have lost their base."""
        self.units = {k: u for k, u in self.units.items() if u.alive}
        self.buildings = {k: b for k, b in self.buildings.items() if b.alive}
        for player in self.players.values():
            if player.alive and not self.has_base(player.pid):
                player.alive = False

    def nodes_of(self, pid: int) -> int:
        return sum(1 for owner in self.node_owner.values() if owner == pid)

    def to_wire(self) -> dict:
        return {
            "turn": self.turn,
            "nodes": [[list(t), o] for t, o in sorted(self.node_owner.items())],
            "units": [u.to_wire() for u in self.units.values() if u.alive],
            "buildings": [b.to_wire() for b in self.buildings.values() if b.alive],
            "players": [p.to_wire() for p in self.players.values()],
        }
