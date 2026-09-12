"""The battlefield: a tile grid, loaded from hand-authored ASCII maps.

Maps are text files so that making a new one needs a text editor and nothing
else. That matters more than it sounds: this game has no procedural
generation, so its variety comes entirely from how cheap it is to write
another map.

Movement is four-directional on purpose. Diagonals would make chokepoints
leaky and the pathfinding fiddlier, and a chokepoint that actually holds is
the whole point of putting a mountain range on a map.
"""

from __future__ import annotations

import heapq
import os
from dataclasses import dataclass

# Tile glyphs, as they appear in a .map file.
OPEN = "."
ROCK = "#"
WATER = "~"
FOREST = "%"
NODE = "$"
SPAWNS = "12345678"

#: Movement points charged for entering a tile.
#:
#: The scale is deliberately coarse -- eight points per beat at full pace --
#: so that a cautious advance at seven-eighths pace is still whole numbers.
#: See resolve.MOVE_PACE, which must satisfy MOVE_PACE * SUBTICKS == COST_OPEN.
COST_OPEN = 96
COST_FOREST = 192

#: The largest map the game will load.
#:
#: This began at 32x24, the exact size the board viewport can show, with an
#: error that said "exceeds what the screen can show without scrolling". Then
#: the client learned to scroll and it became a question of play and memory
#: rather than pixels.
#:
#: It is now a *world* rather than a battlefield: several territories on one
#: map, with one clock over all of them, simulated only where something is
#: happening. The ceiling is what a Pi 400 can hold -- the client keeps a
#: pre-composited terrain surface and a fog surface, both 14 bytes-ish a tile,
#: so 256x160 is about 40,000 tiles and roughly 45MB of surfaces. Past that
#: the client has to composite terrain per region too, which is work nobody
#: has needed yet.
MAX_W = 256
MAX_H = 160

_PASSABLE = set(OPEN + FOREST + NODE + SPAWNS)
_BLOCKS_SIGHT = {ROCK, FOREST}

#: Four-way neighbourhood, in a fixed order so pathfinding is deterministic.
NEIGHBOURS = ((0, -1), (1, 0), (0, 1), (-1, 0))


#: Human-readable terrain, for the hover tooltip. New players should never
#: have to work out what a coloured square means by experiment.
TERRAIN_INFO = {
    OPEN: ("Open ground", "Easy going."),
    ROCK: ("Rock", "Impassable. Blocks line of sight."),
    WATER: ("Water", "Impassable. You can see across it."),
    FOREST: ("Forest", "Blocks sight. Costs double to cross."),
    NODE: ("Resource node", "Stand on it once to claim it."),
}


def describe(char: str) -> tuple[str, str]:
    if char in SPAWNS:
        return ("Starting position", "Where a commander began.")
    return TERRAIN_INFO.get(char, ("Open ground", ""))


class MapError(Exception):
    """A map file that cannot be used as written."""


def _pace(raw, name: str) -> int:
    """Read a ``!pace`` header, refusing anything that is not a whole step."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PACE
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise MapError(f"{name}: pace must be a whole number, got '{raw}'")
    if not 1 <= value <= MAX_PACE:
        raise MapError(f"{name}: pace must be 1..{MAX_PACE}, got {value}")
    return value


#: How much faster armies move, per map. A big map with ordinary movement is
#: not a strategic map, it is the same match with more walking in it: four
#: times the area is twice the distance, so every march takes twice as long
#: and the median match doubles. A map declares its own tempo instead, so the
#: extra ground buys fog and room to manoeuvre rather than waiting.
#:
#: Whole numbers only. Movement arithmetic is integral everywhere so that a Pi
#: and a desktop cannot disagree about where a unit ended up, and a fractional
#: multiplier would put a stop to that.
DEFAULT_PACE = 1
MAX_PACE = 4


@dataclass(frozen=True)
class MapInfo:
    name: str
    author: str
    players: int
    teams: bool
    notes: str
    pace: int = DEFAULT_PACE


class TileMap:
    """A rectangular grid of terrain, plus spawn points and resource nodes."""

    __slots__ = ("width", "height", "tiles", "spawns", "nodes", "node_set",
                 "info", "step_cost")

    def __init__(self, tiles: list[str], spawns: dict[int, tuple[int, int]],
                 nodes: list[tuple[int, int]], info: MapInfo) -> None:
        self.tiles = tiles
        self.height = len(tiles)
        self.width = len(tiles[0]) if tiles else 0
        self.spawns = spawns
        self.nodes = nodes
        #: The same tiles as a set. One source of truth for "is this a node",
        #: so capture and harvesting can never disagree about it.
        self.node_set = set(nodes)
        self.info = info
        #: Passable tiles mapped to what they cost to enter, flattened once at
        #: load. A* asked ``passable()`` and ``cost()`` for every neighbour it
        #: looked at, which is six Python calls a step and was far and away the
        #: most expensive thing in the game -- 46% of a self-play match, and
        #: the thing a Pi 400 feels most. One dict lookup answers both, and an
        #: absent key means "off the map or impassable", exactly as before.
        self.step_cost = {
            (x, y): (COST_FOREST if row[x] == FOREST else COST_OPEN)
            for y, row in enumerate(self.tiles)
            for x in range(len(row))
            if row[x] in _PASSABLE
        }

    # -- queries -----------------------------------------------------------
    def at(self, x: int, y: int) -> str:
        if not self.inside(x, y):
            return ROCK
        return self.tiles[y][x]

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def passable(self, x: int, y: int) -> bool:
        return self.inside(x, y) and self.tiles[y][x] in _PASSABLE

    def blocks_sight(self, x: int, y: int) -> bool:
        return not self.inside(x, y) or self.tiles[y][x] in _BLOCKS_SIGHT

    def cost(self, x: int, y: int) -> int:
        return COST_FOREST if self.at(x, y) == FOREST else COST_OPEN

    def is_node(self, x: int, y: int) -> bool:
        return (x, y) in self.node_set

    # -- loading -----------------------------------------------------------
    @classmethod
    def parse(cls, text: str, name: str = "map") -> "TileMap":
        """Read a .map file.

        Lines beginning with ``!`` are metadata (``!name``, ``!players`` and so
        on); ``#`` at the start of a line would be ambiguous with rock, so
        comments use ``;``.
        """
        meta: dict[str, str] = {}
        rows: list[str] = []
        for raw in text.splitlines():
            line = raw.rstrip("\n")
            if line.startswith(";") or (not line.strip() and not rows):
                continue
            if line.startswith("!"):
                key, _, value = line[1:].partition(" ")
                meta[key.strip().lower()] = value.strip()
                continue
            if not line.strip():
                continue
            rows.append(line)

        if not rows:
            raise MapError(f"{name}: no map rows found")
        width = max(len(row) for row in rows)
        # Pad short rows rather than rejecting them -- trailing spaces get
        # eaten by editors constantly and it is a miserable thing to debug.
        rows = [row.ljust(width, ROCK) for row in rows]

        if width > MAX_W or len(rows) > MAX_H:
            raise MapError(
                f"{name}: {width}x{len(rows)} exceeds the "
                f"{MAX_W}x{MAX_H} a world may be")

        spawns: dict[int, tuple[int, int]] = {}
        nodes: list[tuple[int, int]] = []
        for y, row in enumerate(rows):
            for x, char in enumerate(row):
                if char in SPAWNS:
                    slot = int(char)
                    if slot in spawns:
                        raise MapError(f"{name}: duplicate spawn '{char}'")
                    spawns[slot] = (x, y)
                elif char == NODE:
                    nodes.append((x, y))
                elif char not in _PASSABLE and char not in (ROCK, WATER):
                    raise MapError(f"{name}: unknown tile '{char}' at {x},{y}")

        if len(spawns) < 2:
            raise MapError(f"{name}: needs at least two spawn points")
        expected = set(range(1, len(spawns) + 1))
        if set(spawns) != expected:
            raise MapError(f"{name}: spawns must be numbered 1..{len(spawns)}")

        info = MapInfo(
            name=meta.get("name", name),
            author=meta.get("author", ""),
            players=int(meta.get("players", len(spawns))),
            teams=meta.get("teams", "").lower() in ("1", "true", "yes"),
            notes=meta.get("notes", ""),
            pace=_pace(meta.get("pace"), name),
        )
        return cls(rows, spawns, nodes, info)

    @classmethod
    def load(cls, path: str) -> "TileMap":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.parse(handle.read(), os.path.basename(path))

    def to_wire(self) -> dict:
        return {"tiles": self.tiles, "name": self.info.name,
                "players": self.info.players, "teams": self.info.teams,
                "notes": self.info.notes, "author": self.info.author,
                "pace": self.info.pace}

    @classmethod
    def from_wire(cls, data: dict) -> "TileMap":
        """Rebuild a map a client was sent.

        The wire carries tiles and metadata separately, and only the tiles go
        through the parser -- so every ``!`` header has to be reapplied here or
        it is silently lost. Pace found this the hard way: the server ran a map
        at double speed and every client believed it was at single.
        """
        tilemap = cls.parse("\n".join(data["tiles"]), data.get("name", "map"))
        tilemap.info = MapInfo(
            name=data.get("name", tilemap.info.name),
            author=data.get("author", tilemap.info.author),
            players=int(data.get("players", tilemap.info.players)),
            teams=bool(data.get("teams", tilemap.info.teams)),
            notes=data.get("notes", tilemap.info.notes),
            pace=_pace(data.get("pace"), data.get("name", "map")),
        )
        return tilemap


# ---------------------------------------------------------------------------
# Pathfinding and sight
# ---------------------------------------------------------------------------

def find_path(tilemap: TileMap, start: tuple[int, int], goal: tuple[int, int],
              blocked: set | None = None, limit: int = 0) -> list:
    """A* from ``start`` to ``goal``, returning the tiles after ``start``.

    ``blocked`` holds tiles occupied by other units and buildings. They are
    treated as walls when planning, but a unit that finds its way barred at
    execution time simply stops -- the world moves between planning and doing,
    which is the whole tension of a simultaneous-turn game.

    Returns ``[]`` when the goal is unreachable, and a path toward the nearest
    reachable tile is *not* attempted: a unit that cannot get there should sit
    still and let the player see that, rather than wandering somewhere they
    did not ask for.
    """
    if start == goal or not tilemap.passable(*goal):
        return []
    blocked = blocked or set()
    if goal in blocked:
        return []
    # The cap exists so a hopeless search cannot stall a turn, and it has to
    # scale with the board: a flat 4000 was ample for a 32x24 map and silently
    # broke a 224x128 world, where a corner-to-corner march explores far more
    # than that and simply gave up. A unit that quietly refuses to walk across
    # the world is a much worse bug than a slow path.
    if limit <= 0:
        limit = max(4000, tilemap.width * tilemap.height)

    # Everything the inner loop touches is bound to a local first. At a few
    # hundred thousand iterations a match, attribute lookups are the cost.
    step_cost = tilemap.step_cost
    goal_x, goal_y = goal
    push, pop = heapq.heappush, heapq.heappop
    far = 1 << 30

    start_h = (abs(start[0] - goal_x) + abs(start[1] - goal_y)) * COST_OPEN
    open_heap = [(start_h, 0, start)]
    came: dict = {start: None}
    best: dict = {start: 0}
    seen = 0

    while open_heap:
        _, spent, current = pop(open_heap)
        if current == goal:
            break
        if spent > best.get(current, far):
            continue
        seen += 1
        if seen > limit:
            return []
        cx, cy = current
        for dx, dy in NEIGHBOURS:
            nx, ny = cx + dx, cy + dy
            nxt = (nx, ny)
            step = step_cost.get(nxt)
            if step is None or nxt in blocked:
                continue
            cost = spent + step
            if cost < best.get(nxt, far):
                best[nxt] = cost
                came[nxt] = current
                push(open_heap, (cost + (abs(nx - goal_x) + abs(ny - goal_y))
                                 * COST_OPEN, cost, nxt))

    if goal not in came:
        return []
    path = []
    node = goal
    while node != start:
        path.append(node)
        node = came[node]
    path.reverse()
    return path


def line_of_sight(tilemap: TileMap, a: tuple[int, int], b: tuple[int, int]) -> bool:
    """Can a unit at ``a`` see tile ``b``?

    Bresenham, with the target tile itself exempt from blocking -- you can see
    the edge of a wood, you just cannot see through it.
    """
    x0, y0 = a
    x1, y1 = b
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    while True:
        if (x0, y0) == (x1, y1):
            return True
        if (x0, y0) != a and tilemap.blocks_sight(x0, y0):
            return False
        err2 = 2 * err
        if err2 > -dy:
            err -= dy
            x0 += sx
        if err2 < dx:
            err += dx
            y0 += sy


def visible_tiles(tilemap: TileMap, origin: tuple[int, int], radius: int) -> set:
    """Every tile within ``radius`` of ``origin`` with clear line of sight."""
    ox, oy = origin
    out = set()
    rr = radius * radius
    for y in range(max(0, oy - radius), min(tilemap.height, oy + radius + 1)):
        for x in range(max(0, ox - radius), min(tilemap.width, ox + radius + 1)):
            if (x - ox) ** 2 + (y - oy) ** 2 > rr:
                continue
            if line_of_sight(tilemap, origin, (x, y)):
                out.add((x, y))
    return out


def chebyshev(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])
