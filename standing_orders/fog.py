"""Fog of war.

The server holds the only complete picture and hands each player a timeline
containing just the parts their side could actually see. That makes fog a
filtering problem rather than a netcode problem: there is no live state to
leak, because clients never simulate anything -- they only ever replay what
they were sent.

Vision is recomputed as units move during a turn, so an ambush is revealed at
the beat your scout walked into it, not retroactively at the end.
"""

from __future__ import annotations

from .grid import visible_tiles

#: Events every player sees regardless of where they happened. Eliminations
#: are public knowledge; a player's own income is sent only to them.
ALWAYS_PUBLIC = frozenset({"eliminated"})
PRIVATE_TO_OWNER = frozenset({"income"})


class VisionCache:
    """Memoises line-of-sight discs for one turn.

    Most units stand still for most beats, and a unit that has not moved sees
    exactly what it saw last beat. Without this the per-beat fog recomputation
    is the most expensive thing in the game; with it, it barely registers.
    """

    __slots__ = ("_map", "_cache")

    def __init__(self, tilemap) -> None:
        self._map = tilemap
        self._cache: dict = {}

    def disc(self, tile, radius: int) -> frozenset:
        key = (tile, radius)
        got = self._cache.get(key)
        if got is None:
            got = frozenset(visible_tiles(self._map, tile, radius))
            self._cache[key] = got
        return got


def team_vision(state, team: int, cache: VisionCache) -> frozenset:
    """Every tile the given team can currently see."""
    seen: set = set()
    for unit in state.units.values():
        if unit.alive and state.team_of(unit.owner) == team:
            seen |= cache.disc(unit.tile, unit.type.vision)
    for building in state.buildings.values():
        if building.alive and state.team_of(building.owner) == team:
            seen |= cache.disc(building.tile, building.type.vision)
    return frozenset(seen)


def _tiles_of(event: dict) -> list:
    """Tiles an event should be judged visible by."""
    tiles = []
    for key in ("at", "to"):
        value = event.get(key)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            tiles.append((int(value[0]), int(value[1])))
    return tiles


def filter_events(events: list, vision_by_beat: dict, viewer_team: int,
                  viewer_pid: int, owner_of_unit: dict) -> list:
    """Cut a turn's timeline down to what one player witnessed.

    An event survives if any tile it touches was visible to the viewer's team
    on that beat, or if the acting unit belongs to the viewer's side -- you
    always know what your own army did, even in the dark.
    """
    out = []
    for event in events:
        kind = event["e"]
        if kind in ALWAYS_PUBLIC:
            out.append(event)
            continue
        if kind in PRIVATE_TO_OWNER:
            if event.get("pid") == viewer_pid:
                out.append(event)
            continue

        actor = event.get("uid") if kind != "found" else event.get("bid")
        if kind in ("found", "ready", "strike"):
            actor = event.get("bid", actor)
        if owner_of_unit.get((kind, actor)) == viewer_team:
            out.append(event)
            continue

        visible = vision_by_beat.get(event["s"], frozenset())
        if any(tile in visible for tile in _tiles_of(event)):
            out.append(event)
    return out


def visible_state(state, viewer_pid: int, vision: frozenset) -> dict:
    """The end-of-turn snapshot one player is allowed to see."""
    team = state.team_of(viewer_pid)
    units = []
    for unit in state.units.values():
        if not unit.alive:
            continue
        friendly = state.team_of(unit.owner) == team
        if not friendly and unit.tile not in vision:
            continue
        wire = unit.to_wire()
        if not friendly:
            # Seeing a unit tells you where it is, not what it was told to do.
            wire.pop("path", None)
            wire.pop("stance", None)
            # Rank stays: an enemy officer is meant to be a visible target.
            # Whether they are due a promotion is not your business.
            wire.pop("blooded", None)
        units.append(wire)
    buildings = []
    for building in state.buildings.values():
        if not building.alive:
            continue
        if state.team_of(building.owner) == team or building.tile in vision:
            wire = building.to_wire()
            if state.team_of(building.owner) == team:
                wire["stalled"] = state.site_is_stalled(building)
            buildings.append(wire)
    return {
        "turn": state.turn,
        "nodes": [[list(t), o] for t, o in sorted(state.node_owner.items())],
        "units": units,
        "buildings": buildings,
        "vision": sorted(vision),
        "players": [p.to_wire() for p in state.players.values()],
        "supply": state.players[viewer_pid].supply if viewer_pid in state.players else 0,
        "cap": state.army_cap_of(viewer_pid),
        "army": state.army_size(viewer_pid),
    }
