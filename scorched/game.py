"""Game rules: players, rounds, turn order, economy.

This module is the authority.  Clients render what it says and send it requests;
it never trusts anything a client claims about the world.  That keeps a laggy
Pi and a fast desktop honest with each other, and it means a malformed message
can at worst waste a turn rather than corrupt the match.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from . import weapons as W
from .physics import Simulation, DEFAULT_GRAVITY, WALL_MODES
from .terrain import STYLES, Terrain, WORLD_W

MAX_PLAYERS = 8
START_HP = 100

#: Eight readable, clearly distinct tank colours -- deliberately a limited
#: palette so the whole thing keeps its lo-fi look.
TEAM_COLORS = (
    (232, 64, 48),    # red
    (72, 148, 255),   # blue
    (96, 208, 88),    # green
    (248, 208, 64),   # yellow
    (208, 96, 224),   # magenta
    (96, 224, 216),   # cyan
    (248, 152, 56),   # orange
    (216, 216, 216),  # white
)
COLOR_NAMES = ("Red", "Blue", "Green", "Yellow", "Magenta", "Cyan", "Orange", "White")

PHASE_LOBBY = "lobby"
PHASE_BUY = "buy"
PHASE_AIM = "aim"
PHASE_RESOLVE = "resolve"
PHASE_ROUND_END = "roundend"
PHASE_GAME_OVER = "gameover"


@dataclass
class Settings:
    rounds: int = 5
    start_cash: int = 12000
    turn_time: int = 45            # 0 disables the clock
    buy_time: int = 60
    wind_max: int = 60             # 0 for still air
    wind_changes: bool = True      # re-roll wind every turn, not every round
    gravity: float = DEFAULT_GRAVITY
    terrain_style: str = "random"
    wall_mode: str = "none"
    fuel_per_round: int = 150
    cash_per_damage: int = 30
    kill_bonus: int = 3000
    suicide_penalty: int = 2500
    sudden_death: int = 55         # turns before the round starts killing; 0 = off

    def clamp(self) -> "Settings":
        self.rounds = max(1, min(50, int(self.rounds)))
        self.start_cash = max(0, min(200000, int(self.start_cash)))
        self.turn_time = max(0, min(300, int(self.turn_time)))
        self.buy_time = max(10, min(600, int(self.buy_time)))
        self.wind_max = max(0, min(200, int(self.wind_max)))
        self.gravity = max(0.02, min(0.6, float(self.gravity)))
        if self.terrain_style not in STYLES:
            self.terrain_style = "random"
        if self.wall_mode not in WALL_MODES:
            self.wall_mode = "none"
        self.fuel_per_round = max(0, min(1000, int(self.fuel_per_round)))
        self.sudden_death = max(0, min(999, int(self.sudden_death)))
        return self

    def to_wire(self) -> dict:
        return {
            "rounds": self.rounds, "start_cash": self.start_cash,
            "turn_time": self.turn_time, "buy_time": self.buy_time,
            "wind_max": self.wind_max, "wind_changes": self.wind_changes,
            "gravity": self.gravity, "terrain_style": self.terrain_style,
            "wall_mode": self.wall_mode, "fuel_per_round": self.fuel_per_round,
            "sudden_death": self.sudden_death,
        }

    @classmethod
    def from_wire(cls, data: dict) -> "Settings":
        s = cls()
        for key, value in (data or {}).items():
            if hasattr(s, key):
                setattr(s, key, value)
        return s.clamp()


@dataclass
class Player:
    pid: int
    name: str
    color: int = 0
    bot: bool = False
    skill: str = "moderate"
    connected: bool = True
    ready: bool = False

    # Per-round state
    hp: int = START_HP
    max_hp: int = START_HP
    shield_hp: int = 0
    shield_max: int = 0
    x: float = 0.0
    y: float = 0.0
    angle: int = 45
    power: int = 500
    weapon: str = "bmis"
    fuel: int = 0
    parachutes: int = 0
    alive: bool = True
    done_buying: bool = False

    # Persistent
    cash: int = 0
    score: int = 0
    wins: int = 0
    inv: W.Inventory = field(default_factory=W.Inventory.starting)

    def reset_for_round(self, settings: Settings) -> None:
        self.hp = self.max_hp = START_HP
        self.shield_hp = 0
        self.shield_max = 0
        self.alive = True
        self.fuel = settings.fuel_per_round
        self.parachutes = self.inv.item_count("para")
        self.angle = 45 if self.color % 2 == 0 else 135
        self.power = 500
        self.done_buying = False
        if not self.inv.has(self.weapon):
            self.weapon = "bmis"

    def to_wire(self, full: bool = True) -> dict:
        data = {
            "pid": self.pid, "name": self.name, "color": self.color,
            "bot": self.bot, "connected": self.connected, "ready": self.ready,
            "hp": self.hp, "max_hp": self.max_hp, "shield": self.shield_hp,
            "shield_max": self.shield_max, "x": round(self.x, 1),
            "y": round(self.y, 1), "angle": self.angle, "power": self.power,
            "weapon": self.weapon, "fuel": self.fuel,
            "chutes": self.parachutes, "alive": self.alive,
            "cash": self.cash, "score": self.score, "wins": self.wins,
            "buying": not self.done_buying,
        }
        if full:
            data["inv"] = self.inv.to_wire()
        return data


class Game:
    """One match: several rounds, a shop between each."""

    def __init__(self, settings: Settings | None = None, seed: int | None = None) -> None:
        self.settings = (settings or Settings()).clamp()
        self.rng = random.Random(seed)
        self.players: dict[int, Player] = {}
        self.order: list[int] = []
        self.turn_index: int = 0
        self.round: int = 0
        self.phase: str = PHASE_LOBBY
        self.wind: int = 0
        self.terrain: Terrain = Terrain.generate(self.rng.randrange(1 << 30))
        self.deadline: float = 0.0
        self.shot_seq: int = 0
        self.turns_this_round: int = 0
        self.last_shot: dict | None = None
        self.log: list[str] = []
        self._next_pid = 0

    # -- roster ----------------------------------------------------------
    def add_player(self, name: str, bot: bool = False, skill: str = "moderate") -> Player | None:
        if len(self.players) >= MAX_PLAYERS:
            return None
        pid = self._next_pid
        self._next_pid += 1
        used = {p.color for p in self.players.values()}
        color = next((c for c in range(MAX_PLAYERS) if c not in used), 0)
        player = Player(pid=pid, name=_clean_name(name), color=color, bot=bot,
                        skill=skill, cash=self.settings.start_cash)
        player.ready = bot
        self.players[pid] = player
        return player

    def remove_player(self, pid: int) -> None:
        player = self.players.pop(pid, None)
        if player is None:
            return
        if pid in self.order:
            idx = self.order.index(pid)
            self.order.remove(pid)
            if idx < self.turn_index:
                self.turn_index -= 1
            if self.turn_index >= len(self.order) and self.order:
                self.turn_index = 0

    def living(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    # -- match flow ------------------------------------------------------
    def start_match(self) -> None:
        for player in self.players.values():
            player.cash = self.settings.start_cash
            player.score = 0
            player.wins = 0
            player.inv = W.Inventory.starting()
        self.round = 0
        self.log.clear()
        self.start_round()

    def start_round(self) -> None:
        self.round += 1
        self.terrain = Terrain.generate(self.rng.randrange(1 << 30),
                                        self.settings.terrain_style)
        for player in self.players.values():
            player.reset_for_round(self.settings)
        self._place_tanks()
        self.order = [p.pid for p in self.players.values()]
        self.rng.shuffle(self.order)
        self.turn_index = 0
        self.turns_this_round = 0
        self.roll_wind()
        self.phase = PHASE_AIM
        self._arm_turn_clock()

    def roll_wind(self) -> None:
        span = self.settings.wind_max
        self.wind = self.rng.randint(-span, span) if span > 0 else 0

    def _place_tanks(self) -> None:
        """Spread tanks out, and flatten a small pad under each one.

        Without the pad a tank can spawn on a 40-degree slope and look like it
        is sliding off the map, which reads as a bug even though it is not.
        """
        count = len(self.players)
        if count == 0:
            return
        margin = 44
        usable = WORLD_W - margin * 2
        slot = usable / count
        spots: list[int] = []
        for i in range(count):
            centre = margin + slot * (i + 0.5)
            jitter = slot * 0.30
            spots.append(int(centre + self.rng.uniform(-jitter, jitter)))
        self.rng.shuffle(spots)
        for player, x in zip(self.players.values(), spots):
            x = max(12, min(WORLD_W - 13, x))
            level = self.terrain.surface(x)
            self._flatten_pad(x, level)
            player.x = float(x)
            player.y = float(level)
        self.terrain.dirty_lo = 0
        self.terrain.dirty_hi = self.terrain.width

    def _flatten_pad(self, x: int, level: int) -> None:
        """Level the ground under a tank, easing back into the hillside.

        A hard-edged pad leaves sheer one-pixel cliffs either side of every
        tank, which looks like a rendering fault. Ramping the edges out over a
        dozen columns keeps the landscape continuous.
        """
        core, ramp = 5, 12
        heights = self.terrain.height
        for dx in range(-(core + ramp), core + ramp + 1):
            col = x + dx
            if not (0 <= col < self.terrain.width):
                continue
            distance = abs(dx)
            if distance <= core:
                heights[col] = level
                continue
            t = (distance - core) / ramp
            blend = t * t * (3.0 - 2.0 * t)         # smoothstep
            heights[col] = int(round(level + (heights[col] - level) * blend))

    def _arm_turn_clock(self) -> None:
        limit = self.settings.turn_time
        self.deadline = time.monotonic() + limit if limit > 0 else 0.0

    def time_left(self) -> float:
        if self.deadline <= 0:
            return -1.0
        return max(0.0, self.deadline - time.monotonic())

    # -- turns -----------------------------------------------------------
    def advance_turn(self) -> None:
        """Hand the turn to the next living player, ending the round if done."""
        if self.check_round_over():
            return
        for _ in range(len(self.order) + 1):
            self.turn_index = (self.turn_index + 1) % max(1, len(self.order))
            player = self.players.get(self.order[self.turn_index]) if self.order else None
            if player is not None and player.alive:
                break
        self.turns_this_round += 1
        self._sudden_death()
        if self.check_round_over():
            return
        if self.settings.wind_changes:
            self.roll_wind()
        current = self.current_or_none()
        if current is not None:
            # Repair kits are used automatically at the top of your turn -- the
            # original made you remember; this is kinder and just as tactical.
            self._auto_repair(current)
        self.phase = PHASE_AIM
        self._arm_turn_clock()

    def _sudden_death(self) -> None:
        """Stop a stalemate from running forever.

        Once a round has gone on far too long -- usually two dug-in tanks
        lobbing near-misses at each other across a cratered map -- the ground
        itself starts taking sides.  The damage escalates so the end is never
        far away, and it applies to everyone equally.
        """
        limit = self.settings.sudden_death
        if limit <= 0 or self.turns_this_round <= limit:
            return
        over = self.turns_this_round - limit
        amount = 2 + over // 4
        if over == 1:
            self.note("Sudden death: the ground is giving way")
        for player in self.living():
            player.hp = max(0, player.hp - amount)
            if player.hp <= 0:
                player.alive = False
                self.note(f"{player.name} is swallowed by the crater field")

    def current_or_none(self) -> Player | None:
        if not self.order or self.turn_index >= len(self.order):
            return None
        return self.players.get(self.order[self.turn_index])

    def _auto_repair(self, player: Player) -> None:
        """Burn at most one repair kit, and only when genuinely in trouble.

        Healing every turn turns the game into a stalemate, so this is a
        last-ditch top-up rather than sustain.
        """
        if player.hp > player.max_hp * 0.30:
            return
        kit = W.ITEM_BY_CODE["rep"]
        if not player.inv.consume_item("rep"):
            return
        player.hp = min(player.max_hp, player.hp + kit.strength)
        self.note(f"{player.name} patches up (+{kit.strength})")

    def check_round_over(self) -> bool:
        alive = self.living()
        if len(alive) > 1:
            return False
        if self.phase in (PHASE_ROUND_END, PHASE_GAME_OVER):
            return True
        if alive:
            winner = alive[0]
            winner.wins += 1
            winner.score += 5
            winner.cash += 5000
            self.note(f"{winner.name} takes round {self.round}")
        else:
            self.note("Mutual destruction. Nobody takes the round.")
        if self.round >= self.settings.rounds:
            self.phase = PHASE_GAME_OVER
        else:
            self.phase = PHASE_BUY
            for player in self.players.values():
                player.done_buying = player.bot
            self.deadline = time.monotonic() + self.settings.buy_time
        return True

    # -- player actions ---------------------------------------------------
    def set_aim(self, player: Player, angle: int | None = None,
                power: int | None = None) -> None:
        if angle is not None:
            player.angle = max(0, min(180, int(angle)))
        if power is not None:
            player.power = max(0, min(1000, int(power)))

    def set_weapon(self, player: Player, code: str) -> bool:
        if code in W.WEAPON_BY_CODE and player.inv.has(code):
            player.weapon = code
            return True
        return False

    def move(self, player: Player, direction: int) -> bool:
        """Drive one step left or right; costs fuel and refuses cliffs."""
        if player.fuel <= 0 or not player.alive:
            return False
        direction = 1 if direction > 0 else -1
        nx = int(player.x) + direction
        if nx < 10 or nx > WORLD_W - 11:
            return False
        here = self.terrain.surface(int(player.x))
        there = self.terrain.surface(nx)
        climb = here - there
        if climb > 6:                 # too steep to climb
            return False
        cost = 2 if climb > 0 else 1
        if player.fuel < cost:
            return False
        player.fuel -= cost
        player.x = float(nx)
        player.y = float(there)
        return True

    def activate_shield(self, player: Player, code: str) -> bool:
        item = W.ITEM_BY_CODE.get(code)
        if item is None or item.kind != "shield":
            return False
        if player.shield_hp > 0:
            return False
        if not player.inv.consume_item(code):
            return False
        player.shield_hp = item.strength
        player.shield_max = item.strength
        self.note(f"{player.name} raises a {item.name}")
        return True

    def buy(self, player: Player, code: str, qty: int = 1) -> bool:
        price = W.price_of(code)
        pack = W.pack_of(code)
        if price is None or pack <= 0:
            return False
        qty = max(1, min(20, int(qty)))
        total = price * qty
        if player.cash < total:
            return False
        player.cash -= total
        player.inv.add(code, pack * qty)
        return True

    def sell(self, player: Player, code: str) -> bool:
        """Refund at half price -- lets a misclick be undone without a rules
        lawyer, while still costing enough that it is not a free do-over."""
        price = W.price_of(code)
        pack = W.pack_of(code)
        if price is None or pack <= 0:
            return False
        if code in W.WEAPON_BY_CODE:
            if player.inv.ammo.get(code, 0) < pack:
                return False
            player.inv.ammo[code] -= pack
            if player.inv.ammo[code] <= 0:
                del player.inv.ammo[code]
        else:
            if player.inv.items.get(code, 0) < pack:
                return False
            player.inv.items[code] -= pack
            if player.inv.items[code] <= 0:
                del player.inv.items[code]
        player.cash += price // 2
        return True

    # -- firing ------------------------------------------------------------
    def fire(self, player: Player) -> dict | None:
        """Resolve the current player's shot and return the wire timeline."""
        if self.phase != PHASE_AIM or self.current_or_none() is not player:
            return None
        if not player.alive:
            return None
        code = player.weapon
        if not player.inv.has(code):
            code = "bmis"
            player.weapon = code
        if not player.inv.consume(code):
            return None

        tanks = list(self.players.values())
        sim = Simulation(self.terrain, tanks, self.wind, self.settings.gravity,
                         self.settings.wall_mode, seed=self.rng.randrange(1 << 30))
        result = sim.fire(player, player.angle, player.power, code)
        self._settle_economy(player, result)

        self.shot_seq += 1
        self.phase = PHASE_RESOLVE
        self.deadline = 0.0
        payload = {
            "seq": self.shot_seq, "pid": player.pid, "weapon": code,
            "angle": player.angle, "power": player.power, "wind": self.wind,
            "events": result.events, "frames": result.frames,
        }
        self.last_shot = payload
        return payload

    def _settle_economy(self, shooter: Player, result) -> None:
        earned = 0
        for pid, damage in result.damage_dealt.items():
            if pid == shooter.pid:
                earned -= damage * self.settings.cash_per_damage // 2
            else:
                earned += damage * self.settings.cash_per_damage
                shooter.score += damage // 10
        for pid in result.killed:
            victim = self.players.get(pid)
            if victim is None:
                continue
            if pid == shooter.pid:
                earned -= self.settings.suicide_penalty
                shooter.score -= 2
                self.note(f"{shooter.name} manages to kill {shooter.name}")
            else:
                earned += self.settings.kill_bonus
                shooter.score += 3
                self.note(f"{shooter.name} destroys {victim.name}")
        shooter.cash = max(0, shooter.cash + earned)

    def standings(self) -> list[dict]:
        ranked = sorted(self.players.values(),
                        key=lambda p: (p.wins, p.score, p.cash), reverse=True)
        return [
            {"pid": p.pid, "name": p.name, "color": p.color, "wins": p.wins,
             "score": p.score, "cash": p.cash, "bot": p.bot}
            for p in ranked
        ]

    # -- misc --------------------------------------------------------------
    def note(self, text: str) -> None:
        self.log.append(text)
        del self.log[:-40]

    def round_wire(self) -> dict:
        return {
            "t": "round", "round": self.round, "rounds": self.settings.rounds,
            "terrain": self.terrain.to_wire(), "wind": self.wind,
            "players": [p.to_wire() for p in self.players.values()],
            "settings": self.settings.to_wire(),
        }

    def state_wire(self) -> dict:
        current = self.current_or_none()
        return {
            "t": "state", "phase": self.phase, "wind": self.wind,
            "turn": current.pid if (current and self.phase == PHASE_AIM) else -1,
            "round": self.round, "rounds": self.settings.rounds,
            "time": round(self.time_left(), 1),
            "players": [p.to_wire() for p in self.players.values()],
            "terrain_rev": self.shot_seq,
        }


def _clean_name(name: str) -> str:
    name = "".join(ch for ch in str(name) if ch.isprintable()).strip()
    return (name or "Player")[:14]
