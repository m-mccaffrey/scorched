"""Live match state: who owns what, standing where, with how much health."""

from __future__ import annotations

from dataclasses import dataclass, field

from .grid import TileMap
from .units import BUILDING, UNIT

MAX_PLAYERS = 4


@dataclass
class Unit:
    uid: int
    owner: int
    code: str
    x: int
    y: int
    hp: int
    #: Tiles still to walk, nearest first. Recomputed whenever orders change.
    path: list = field(default_factory=list)
    #: "move" | "attack" | "hold"
    stance: str = "hold"
    move_points: int = 0

    @property
    def type(self):
        return UNIT[self.code]

    @property
    def tile(self) -> tuple[int, int]:
        return (self.x, self.y)

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def to_wire(self) -> dict:
        return {"uid": self.uid, "owner": self.owner, "code": self.code,
                "x": self.x, "y": self.y, "hp": self.hp,
                "stance": self.stance, "path": [list(t) for t in self.path]}


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

    def to_wire(self) -> dict:
        return {"bid": self.bid, "owner": self.owner, "code": self.code,
                "x": self.x, "y": self.y, "hp": self.hp,
                "under": self.building_turns,
                "queue": [[c, t] for c, t in self.queue]}


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

    def to_wire(self, full: bool = False) -> dict:
        data = {"pid": self.pid, "name": self.name, "team": self.team,
                "color": self.color, "bot": self.bot, "alive": self.alive,
                "ready": self.ready, "connected": self.connected}
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

    # -- spawning ----------------------------------------------------------
    def add_unit(self, owner: int, code: str, x: int, y: int) -> Unit:
        unit = Unit(uid=self._next_uid, owner=owner, code=code, x=x, y=y,
                    hp=UNIT[code].hp)
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

    def allied(self, a: int, b: int) -> bool:
        return a == b or self.team_of(a) == self.team_of(b)

    def living_teams(self) -> set:
        """Teams that still hold at least one Command Post."""
        teams = set()
        for building in self.buildings.values():
            if building.alive and building.code == "base":
                teams.add(self.team_of(building.owner))
        return teams

    def has_base(self, pid: int) -> bool:
        return any(b.alive and b.code == "base" and b.owner == pid
                   for b in self.buildings.values())

    def owned_building_codes(self, pid: int) -> set:
        return {b.code for b in self.buildings.values()
                if b.owner == pid and b.operational}

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
