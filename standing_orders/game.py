"""Match rules: setup, phases, turn flow, victory.

The server owns one of these. Clients never hold a Match -- they hold whatever
fogged view of it the server last sent them.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .fog import VisionCache, filter_events, team_vision, visible_state
from .grid import TileMap
from .resolve import SUBTICKS, resolve_turn
from .state import MAX_PLAYERS, MatchState, Player
from .units import BUILDING, UNIT

PHASE_LOBBY = "lobby"
PHASE_ORDERS = "orders"
PHASE_RESOLVE = "resolve"
PHASE_OVER = "over"

MAPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps")

#: What each player starts a match holding, beyond their Command Post.
#: Two Engineers, because the economy cannot start without them.
OPENING_UNITS = ("worker", "worker", "trooper", "scout")


@dataclass
class Settings:
    map_name: str = "duel"
    teams: bool = False           # 2v2 when four players; free-for-all otherwise
    start_supply: int = 20
    #: Seconds to write orders; 0 disables the clock. Two minutes rather than
    #: ninety seconds because a world has more than one front in it, and
    #: deciding which front gets your attention this turn is part of the game
    #: rather than an obstacle to it. The turn still resolves the moment
    #: everyone commits, so a quiet turn costs nobody the full two minutes.
    order_time: int = 120

    def clamp(self) -> "Settings":
        self.start_supply = max(0, min(200, int(self.start_supply)))
        self.order_time = max(0, min(600, int(self.order_time)))
        self.teams = bool(self.teams)
        return self

    def to_wire(self) -> dict:
        return {"map_name": self.map_name, "teams": self.teams,
                "start_supply": self.start_supply, "order_time": self.order_time}

    @classmethod
    def from_wire(cls, data: dict) -> "Settings":
        settings = cls()
        for key, value in (data or {}).items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        return settings.clamp()


def available_maps() -> list[str]:
    try:
        names = [f[:-4] for f in os.listdir(MAPS_DIR) if f.endswith(".map")]
    except OSError:
        return ["duel"]
    return sorted(names) or ["duel"]


def load_map(name: str) -> TileMap:
    safe = os.path.basename(str(name)) or "duel"
    path = os.path.join(MAPS_DIR, f"{safe}.map")
    if not os.path.exists(path):
        path = os.path.join(MAPS_DIR, "duel.map")
    return TileMap.load(path)


class Match:
    """One game of Standing Orders, from lobby to victory."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = (settings or Settings()).clamp()
        self.state = MatchState(load_map(self.settings.map_name))
        self.phase = PHASE_LOBBY
        self.deadline: float = 0.0
        self.turn_seq: int = 0
        self.pending: dict[int, list] = {}     # pid -> submitted orders
        self.log: list[str] = []
        self.last_timelines: dict[int, dict] = {}
        self.winner_team: int | None = None
        #: True when the war ended by agreement rather than by annihilation.
        self.peace = False
        self._next_pid = 0

    # -- roster ------------------------------------------------------------
    def add_player(self, name: str, bot: bool = False,
                   skill: str = "moderate") -> Player | None:
        if len(self.state.players) >= MAX_PLAYERS:
            return None
        pid = self._next_pid
        self._next_pid += 1
        used = {p.color for p in self.state.players.values()}
        color = next((c for c in range(MAX_PLAYERS) if c not in used), 0)
        player = Player(pid=pid, name=self._unique_name(_clean(name)),
                        color=color, team=pid, bot=bot, skill=skill, ready=bot)
        self.state.players[pid] = player
        self._reassign_teams()
        return player

    def _unique_name(self, name: str) -> str:
        """Disambiguate a duplicate. Two commanders called Ada is confusing in
        a game whose entire interface is colour-coded by player."""
        taken = {p.name for p in self.state.players.values()}
        if name not in taken:
            return name
        for suffix in range(2, 10):
            candidate = f"{name[:12]} {suffix}"
            if candidate not in taken:
                return candidate
        return name[:12] + " *"

    def remove_player(self, pid: int) -> None:
        self.state.players.pop(pid, None)
        self.pending.pop(pid, None)
        if self.phase == PHASE_LOBBY:
            self._reassign_teams()

    def _reassign_teams(self) -> None:
        """Free-for-all gives everyone their own team; 2v2 splits the roster."""
        players = sorted(self.state.players.values(), key=lambda p: p.pid)
        if self.settings.teams and len(players) == 4:
            for index, player in enumerate(players):
                player.team = index % 2
        else:
            for player in players:
                player.team = player.pid

    def set_teams(self, enabled: bool) -> None:
        self.settings.teams = bool(enabled)
        self._reassign_teams()

    # -- match flow --------------------------------------------------------
    def start_match(self) -> None:
        # Grab the roster before swapping in a fresh board -- it lives on the
        # state object we are about to replace.
        roster = sorted(self.state.players.values(), key=lambda p: p.pid)
        self.state = MatchState(load_map(self.settings.map_name))
        for player in roster:
            self.state.players[player.pid] = player
        self._reassign_teams()
        self._seed_pacts()
        self._place_starts()
        self.turn_seq = 0
        self.state.turn = 1
        self.winner_team = None
        self.peace = False
        self.log.clear()
        self.phase = PHASE_ORDERS
        self.pending = {}
        self._arm_clock()
        self.note(self.state.map.info.name)

    def _seed_pacts(self) -> None:
        """Turn the starting teams into standing alliances.

        Teams decide who begins the war on whose side and nothing after that.
        From here it is all agreements, which can be signed and broken.
        """
        self.state.pacts.clear()
        self.state.offers.clear()
        self.state.breaking.clear()
        roster = sorted(self.state.players.values(), key=lambda p: p.pid)
        for index, player in enumerate(roster):
            for other in roster[index + 1:]:
                if player.team == other.team:
                    self.state.pacts[self.state.pair(player.pid, other.pid)] = \
                        "alliance"

    def _place_starts(self) -> None:
        players = sorted(self.state.players.values(), key=lambda p: p.pid)
        spawns = self.state.map.spawns
        for index, player in enumerate(players, start=1):
            spot = spawns.get(index)
            if spot is None:
                player.alive = False
                continue
            player.alive = True
            player.ready = False
            player.supply = self.settings.start_supply
            self.state.add_building(player.pid, "base", spot[0], spot[1])
            for offset, code in enumerate(OPENING_UNITS):
                tile = self._free_near(spot)
                if tile is not None:
                    self.state.add_unit(player.pid, code, tile[0], tile[1])

    def _free_near(self, origin):
        occupied = set(self.state.occupancy())
        for radius in (1, 2, 3):
            best = None
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    tile = (origin[0] + dx, origin[1] + dy)
                    if self.state.map.passable(*tile) and tile not in occupied:
                        if best is None or tile < best:
                            best = tile
            if best is not None:
                return best
        return None

    def _arm_clock(self) -> None:
        limit = self.settings.order_time
        self.deadline = time.monotonic() + limit if limit > 0 else 0.0

    def time_left(self) -> float:
        if self.deadline <= 0:
            return -1.0
        return max(0.0, self.deadline - time.monotonic())

    # -- orders ------------------------------------------------------------
    def submit(self, pid: int, orders: list) -> None:
        """Record a player's plan. Re-submitting replaces the previous plan."""
        player = self.state.players.get(pid)
        if player is None or not player.alive or self.phase != PHASE_ORDERS:
            return
        self.pending[pid] = list(orders)[:200]
        player.ready = True

    def unready(self, pid: int) -> None:
        player = self.state.players.get(pid)
        if player is not None and self.phase == PHASE_ORDERS:
            player.ready = False

    def everyone_ready(self) -> bool:
        living = [p for p in self.state.players.values() if p.alive]
        if not living:
            return True
        return all(p.ready or not p.connected for p in living)

    def resolve(self, timelines: bool = True) -> dict:
        """Play the turn out and build each player's fogged timeline.

        ``timelines=False`` plays the same turn by the same rules but skips
        the fog bookkeeping and the per-player event lists, which together are
        most of the cost of a turn nobody is going to watch. Used by the
        offline parameter search, which only ever reads the final state.
        """
        orders = dict(self.pending)
        self.pending = {}
        result, rejected = resolve_turn(self.state, orders, vision=timelines)
        self.turn_seq += 1
        if not timelines:
            self.last_timelines = {}
            for pid in result.eliminated:
                player = self.state.players.get(pid)
                if player is not None:
                    self.note(f"{player.name} has been knocked out")
            self.state.turn += 1
            self.phase = PHASE_RESOLVE
            self.deadline = 0.0
            return self.last_timelines

        owner_lookup = {}
        for unit in self.state.units.values():
            owner_lookup[("move", unit.uid)] = self.state.bloc_of(unit.owner)
        for event in result.events:
            actor = event.get("uid")
            kind = event["e"]
            if kind in ("move", "block", "shoot", "spawn", "promote", "heal"):
                unit = self.state.units.get(actor)
                if unit is not None:
                    owner_lookup[(kind, actor)] = self.state.bloc_of(unit.owner)
            elif kind in ("found", "ready", "strike"):
                building = self.state.buildings.get(event.get("bid"))
                if building is not None:
                    owner_lookup[(kind, building.bid)] = \
                        self.state.bloc_of(building.owner)

        cache = VisionCache(self.state.map)
        self.last_timelines = {}
        for player in self.state.players.values():
            team = self.state.bloc_of(player.pid)
            per_beat = result.vision.get(team, {})
            events = filter_events(result.events, per_beat, team, player.pid,
                                   owner_lookup)
            # A unit can walk into view mid-turn, and the client has never
            # heard of it. Ship the identity of everyone who appears in the
            # events this player actually receives, so the replay can draw
            # them -- and nobody else, which would leak the fog away.
            actors = {}
            for event in events:
                for key in ("uid", "tgt"):
                    ident = event.get(key)
                    unit = self.state.units.get(ident) if ident else None
                    if unit is not None:
                        actors[str(unit.uid)] = {"owner": unit.owner,
                                                 "code": unit.code,
                                                 "hp": unit.hp}
            end_vision = per_beat.get(SUBTICKS) or team_vision(
                self.state, team, cache)
            self.last_timelines[player.pid] = {
                "seq": self.turn_seq,
                "turn": self.state.turn,
                "subticks": result.subticks,
                "events": events,
                "actors": actors,
                "state": visible_state(self.state, player.pid, end_vision),
                "rejected": rejected.get(player.pid, []),
            }

        for pid in result.eliminated:
            player = self.state.players.get(pid)
            if player is not None:
                self.note(f"{player.name} has been knocked out")

        self.state.turn += 1
        self.phase = PHASE_RESOLVE
        self.deadline = 0.0
        return self.last_timelines

    def begin_orders(self) -> bool:
        """Open the next order phase, or end the match. True if play goes on."""
        if self.check_over():
            return False
        for player in self.state.players.values():
            player.ready = not player.alive
        self.phase = PHASE_ORDERS
        self._arm_clock()
        return True

    def check_over(self) -> bool:
        """A war ends when one side is left, or when nobody is still fighting.

        The second is the one that makes this a war rather than a deathmatch.
        Most wars are not fought to the extinction of one side; they stop
        because everyone still standing has agreed to stop, and whatever the
        map looks like at that moment is the result.
        """
        living = [p for p in self.state.players.values() if p.alive]
        if self.state.armistice_agreed():
            self.phase = PHASE_OVER
            self.peace = True
            # Peace is a settlement: everyone keeps what they hold, and
            # whoever holds most has won the war without finishing it.
            scores = {}
            for player in living:
                bloc = self.state.bloc_of(player.pid)
                scores[bloc] = self.state.bloc_holding(player.pid)
            self.winner_team = max(scores, key=lambda b: (scores[b], -b))
            names = ", ".join(sorted(
                p.name for p in living
                if self.state.bloc_of(p.pid) == self.winner_team))
            self.note(f"Armistice. {names} hold the most ground")
            return True

        teams = self.state.living_teams()
        if len(teams) > 1:
            return False
        self.phase = PHASE_OVER
        self.winner_team = next(iter(teams)) if teams else None
        if self.winner_team is None:
            self.note("Everyone is destroyed. Nobody wins.")
        else:
            names = ", ".join(p.name for p in self.state.players.values()
                              if p.alive
                              and self.state.bloc_of(p.pid) == self.winner_team)
            self.note(f"Victory: {names}")
        return True

    # -- reporting ---------------------------------------------------------
    def standings(self) -> list:
        rows = []
        for player in sorted(self.state.players.values(), key=lambda p: p.pid):
            rows.append({
                "pid": player.pid, "name": player.name, "team": player.team,
                "color": player.color, "alive": player.alive, "bot": player.bot,
                "supply": player.supply,
                "units": len(self.state.units_of(player.pid)),
                "buildings": len(self.state.buildings_of(player.pid)),
            })
        return rows

    def lobby_wire(self) -> dict:
        return {
            "players": [p.to_wire() for p in self.state.players.values()],
            "settings": self.settings.to_wire(),
            "maps": available_maps(),
            "phase": self.phase,
        }

    def status_wire(self) -> dict:
        return {
            "phase": self.phase, "turn": self.state.turn,
            "time": round(self.time_left(), 1),
            "players": [p.to_wire() for p in self.state.players.values()],
            "standings": self.standings(),
        }

    def note(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-30]


def _clean(name: str) -> str:
    name = "".join(ch for ch in str(name) if ch.isprintable()).strip()
    return (name or "Commander")[:14]


def opening_cost() -> int:
    """Sanity helper: what the opening army would have cost if bought."""
    return sum(UNIT[c].cost for c in OPENING_UNITS) + BUILDING["base"].cost
