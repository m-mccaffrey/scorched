"""The authoritative game server.

Runs headless (``python -m scorched server``) or inside the host player's own
client process, which is what makes "Host Game" a single click rather than a
sysadmin task.

Everything the players see is decided here.  Clients send *intent* -- "my angle
is 47", "fire", "buy two Nukes" -- and receive facts.  A client cannot move
another player's tank, cannot award itself money, and cannot disagree about
where a shell landed, because it never simulates anything itself.
"""

from __future__ import annotations

import argparse
import random
import socket
import threading
import time

from . import weapons as W
from .ai import SKILLS, BotBrain
from .discovery import Beacon
from .game import (COLOR_NAMES, MAX_PLAYERS, PHASE_AIM, PHASE_BUY,
                   PHASE_GAME_OVER, PHASE_LOBBY, PHASE_RESOLVE, Game, Settings)
from .protocol import DEFAULT_PORT, PROTOCOL_VERSION, Connection

TICK_HZ = 30
HEARTBEAT = 0.5          # state broadcast cadence while waiting on a player
AIM_MIRROR_HZ = 12       # how often a spectator's view of the turret updates
BOT_THINK = (0.55, 1.25)
ANIM_GRACE = 3.5         # extra seconds allowed for the slowest client
BOT_NAMES = ("Hal", "Bishop", "Marvin", "Skynet", "Deep Thought", "Clu",
             "Wintermute", "Roy", "Ash", "Proteus")


class Link:
    """One connected client."""

    def __init__(self, conn: Connection, addr) -> None:
        self.conn = conn
        self.addr = addr
        self.pid: int | None = None
        self.hello = False
        self.acked_seq = 0
        self.joined_at = time.monotonic()

    def send(self, msg: dict) -> None:
        self.conn.send(msg)


class Server:
    def __init__(self, port: int = DEFAULT_PORT, name: str = "Scorched",
                 settings: Settings | None = None, bots: int = 0,
                 bot_skill: str = "moderate", seed: int | None = None,
                 announce: bool = True) -> None:
        self.port = port
        self.name = name[:24] or "Scorched"
        self.game = Game(settings, seed=seed)
        self.links: list[Link] = []
        self.host_pid: int | None = None
        self.running = threading.Event()
        self.beacon: Beacon | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._rng = random.Random(seed)
        self._brains: dict[int, BotBrain] = {}
        self._bot_at: float = 0.0
        self._resolve_until: float = 0.0
        self._last_state: float = 0.0
        self._last_mirror: float = 0.0
        self._announce = announce
        self.bound_port = port
        self._pending_bots = (bots, bot_skill)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", self.port))
        self._sock.listen(16)
        self.bound_port = self._sock.getsockname()[1]
        self.running.set()
        threading.Thread(target=self._accept_loop, name="accept", daemon=True).start()
        if self._announce:
            self.beacon = Beacon(self._beacon_info)
            self.beacon.start()
        count, skill = self._pending_bots
        for _ in range(count):
            self.add_bot(skill)

    def stop(self) -> None:
        self.running.clear()
        if self.beacon:
            self.beacon.stop()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        for link in list(self.links):
            link.conn.close()

    def serve_forever(self) -> None:
        self.start()
        try:
            self.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def run(self) -> None:
        """Main loop. Cheap enough that a Pi 400 barely notices it."""
        period = 1.0 / TICK_HZ
        while self.running.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:                      # never die mid-match
                self._log(f"tick error: {exc!r}")
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, period - elapsed))

    def _beacon_info(self) -> dict:
        game = self.game
        humans = sum(1 for p in game.players.values() if not p.bot)
        return {
            "name": self.name, "port": self.bound_port, "version": PROTOCOL_VERSION,
            "players": len(game.players), "humans": humans, "max": MAX_PLAYERS,
            "phase": game.phase, "round": game.round, "rounds": game.settings.rounds,
        }

    # -- connections -----------------------------------------------------
    def _accept_loop(self) -> None:
        while self.running.is_set():
            try:
                sock, addr = self._sock.accept()
            except OSError:
                break
            link = Link(Connection(sock, name=str(addr)), addr)
            with self._lock:
                self.links.append(link)

    def _drop(self, link: Link, reason: str = "left") -> None:
        if link in self.links:
            self.links.remove(link)
        link.conn.close()
        pid = link.pid
        if pid is None:
            return
        player = self.game.players.get(pid)
        if player is None:
            return
        if self.game.phase in (PHASE_LOBBY, PHASE_GAME_OVER):
            self.game.remove_player(pid)
        else:
            # Mid-match: hand the tank to the machine rather than deleting a
            # player out of the turn order and confusing everyone still playing.
            player.connected = False
            player.bot = True
            player.skill = "moderate"
            player.name = f"{player.name}*"
            self._brains[pid] = BotBrain(player.skill, self._rng)
            self._log(f"{player.name} dropped; a bot takes the wheel")
        if self.host_pid == pid:
            self.host_pid = None
            self._promote_host()
        self._broadcast_lobby()

    def _promote_host(self) -> None:
        for link in self.links:
            if link.pid is None:
                continue
            player = self.game.players.get(link.pid)
            if player is not None and not player.bot and player.connected:
                self.host_pid = link.pid
                link.send({"t": "host", "host": True})
                return

    def _link_for(self, pid: int) -> Link | None:
        for link in self.links:
            if link.pid == pid:
                return link
        return None

    def broadcast(self, msg: dict, skip: int | None = None) -> None:
        for link in list(self.links):
            if link.pid is not None and link.pid == skip:
                continue
            if not link.hello:
                continue
            link.send(msg)

    # -- main tick -------------------------------------------------------
    def tick(self) -> None:
        self._pump_network()
        game = self.game
        now = time.monotonic()

        if game.phase == PHASE_AIM:
            self._tick_aim(now)
        elif game.phase == PHASE_RESOLVE:
            self._tick_resolve(now)
        elif game.phase == PHASE_BUY:
            self._tick_buy(now)
        elif game.phase == PHASE_GAME_OVER:
            pass

        if now - self._last_state >= HEARTBEAT:
            self._last_state = now
            self.broadcast(game.state_wire())

    def _pump_network(self) -> None:
        with self._lock:
            links = list(self.links)
        for link in links:
            for msg in link.conn.poll():
                if msg.get("t") == "__closed__":
                    with self._lock:
                        self._drop(link, msg.get("reason", "closed"))
                    break
                try:
                    self._handle(link, msg)
                except Exception as exc:
                    self._log(f"bad message from {link.addr}: {exc!r}")

    def _tick_aim(self, now: float) -> None:
        game = self.game
        player = game.current_or_none()
        if player is None:
            return
        if not player.alive:
            game.advance_turn()
            self._announce_turn()
            return

        if player.bot:
            if self._bot_at == 0.0:
                lo, hi = BOT_THINK
                self._bot_at = now + self._rng.uniform(lo, hi)
            elif now >= self._bot_at:
                self._bot_at = 0.0
                self._run_bot(player)
            return

        if game.deadline > 0 and now >= game.deadline:
            # Out of time: the shot goes off as aimed. Harsh, classic, and it
            # keeps a party game from stalling on someone who wandered off.
            self._log(f"{player.name} ran out of time")
            self._do_fire(player)

    def _run_bot(self, player) -> None:
        game = self.game
        brain = self._brains.setdefault(
            player.pid, BotBrain(player.skill, self._rng))
        try:
            action = brain.take_turn(game, player)
        except Exception as exc:
            self._log(f"bot {player.name} failed to think: {exc!r}")
            action = {"angle": 45, "power": 500, "weapon": "bmis", "shield": None}
        if action.get("shield"):
            if game.activate_shield(player, action["shield"]):
                self.broadcast({"t": "shieldup", "pid": player.pid,
                                "sp": player.shield_hp})
        game.set_weapon(player, action.get("weapon", "bmis"))
        game.set_aim(player, action.get("angle"), action.get("power"))
        self.broadcast({"t": "aim", "pid": player.pid, "angle": player.angle,
                        "power": player.power, "weapon": player.weapon})
        self._do_fire(player)

    def _do_fire(self, player) -> None:
        payload = self.game.fire(player)
        if payload is None:
            self.game.advance_turn()
            self._announce_turn()
            return
        payload["t"] = "shot"
        self.broadcast(payload)
        self.broadcast(self.game.state_wire())
        for link in self.links:
            link.acked_seq = max(link.acked_seq, self.game.shot_seq - 1)
        self._resolve_until = (time.monotonic()
                               + payload["frames"] / 60.0 + ANIM_GRACE)

    def _tick_resolve(self, now: float) -> None:
        seq = self.game.shot_seq
        waiting = [l for l in self.links
                   if l.hello and l.pid is not None and l.acked_seq < seq]
        if waiting and now < self._resolve_until:
            return
        # Either everyone finished the animation or the slowest machine has had
        # its grace period. Either way the world moves on.
        if self.game.check_round_over():
            if self.game.phase == PHASE_GAME_OVER:
                self._announce_game_over()
            else:
                self._announce_buy()
            return
        self.game.advance_turn()
        self._announce_turn()

    def _tick_buy(self, now: float) -> None:
        game = self.game
        everyone_done = all(p.done_buying or p.bot or not p.connected
                            for p in game.players.values())
        for player in game.players.values():
            if player.bot and not player.done_buying:
                brain = self._brains.setdefault(
                    player.pid, BotBrain(player.skill, self._rng))
                brain.shop(game, player)
        if everyone_done or (game.deadline > 0 and now >= game.deadline):
            game.start_round()
            self._announce_round()

    # -- announcements ---------------------------------------------------
    def _announce_round(self) -> None:
        self.broadcast(self.game.round_wire())
        self._announce_turn()

    def _announce_turn(self) -> None:
        game = self.game
        player = game.current_or_none()
        self._bot_at = 0.0
        if player is None:
            return
        self.broadcast({
            "t": "turn", "pid": player.pid, "wind": game.wind,
            "time": game.settings.turn_time,
        })
        self.broadcast(game.state_wire())
        self._flush_log()

    def _announce_buy(self) -> None:
        self.broadcast({
            "t": "shop", "catalogue": W.catalogue(),
            "time": self.game.settings.buy_time,
            "round": self.game.round + 1, "rounds": self.game.settings.rounds,
            "standings": self.game.standings(),
        })
        self.broadcast(self.game.state_wire())
        self._flush_log()

    def _announce_game_over(self) -> None:
        self.broadcast({"t": "gameover", "standings": self.game.standings()})
        self._flush_log()

    def _broadcast_lobby(self) -> None:
        self.broadcast({
            "t": "lobby",
            "players": [p.to_wire() for p in self.game.players.values()],
            "settings": self.game.settings.to_wire(),
            "host": self.host_pid if self.host_pid is not None else -1,
            "phase": self.game.phase,
        })

    def _flush_log(self) -> None:
        if self.game.log:
            self.broadcast({"t": "log", "lines": self.game.log[-6:]})

    def _log(self, text: str) -> None:
        print(f"[server] {text}", flush=True)

    # -- bots ------------------------------------------------------------
    def add_bot(self, skill: str = "moderate") -> bool:
        if self.game.phase != PHASE_LOBBY:
            return False
        used = {p.name for p in self.game.players.values()}
        name = next((n for n in BOT_NAMES if n not in used), None)
        if name is None:
            name = f"Bot{len(self.game.players)}"
        skill = skill if skill in SKILLS else "moderate"
        player = self.game.add_player(name, bot=True, skill=skill)
        if player is None:
            return False
        self._brains[player.pid] = BotBrain(skill, self._rng)
        self._broadcast_lobby()
        return True

    # -- message handling -------------------------------------------------
    def _handle(self, link: Link, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "hello":
            self._on_hello(link, msg)
            return
        if not link.hello or link.pid is None:
            return
        player = self.game.players.get(link.pid)
        if player is None:
            return
        game = self.game
        is_host = (link.pid == self.host_pid)
        is_turn = (game.phase == PHASE_AIM
                   and game.current_or_none() is player)

        if kind == "ping":
            link.send({"t": "pong", "ts": msg.get("ts")})

        elif kind == "setup":
            if game.phase == PHASE_LOBBY:
                if "name" in msg:
                    player.name = str(msg["name"])[:14] or player.name
                if "color" in msg:
                    self._set_color(player, int(msg["color"]))
                if "ready" in msg:
                    player.ready = bool(msg["ready"])
                self._broadcast_lobby()

        elif kind == "settings" and is_host and game.phase == PHASE_LOBBY:
            game.settings = Settings.from_wire(msg.get("settings", {}))
            self._broadcast_lobby()

        elif kind == "addbot" and is_host:
            self.add_bot(str(msg.get("skill", "moderate")))

        elif kind == "kick" and is_host and game.phase == PHASE_LOBBY:
            target = game.players.get(int(msg.get("pid", -1)))
            if target is not None and target.bot:
                game.remove_player(target.pid)
                self._brains.pop(target.pid, None)
                self._broadcast_lobby()

        elif kind == "start" and is_host and game.phase == PHASE_LOBBY:
            if len(game.players) >= 2:
                game.start_match()
                self._announce_round()
            else:
                link.send({"t": "error", "msg": "Need at least two players"})

        elif kind == "restart" and is_host and game.phase == PHASE_GAME_OVER:
            game.phase = PHASE_LOBBY
            for p in game.players.values():
                p.ready = p.bot
            self._broadcast_lobby()

        elif kind == "aim" and is_turn:
            game.set_aim(player, msg.get("angle"), msg.get("power"))
            now = time.monotonic()
            if now - self._last_mirror >= 1.0 / AIM_MIRROR_HZ:
                self._last_mirror = now
                self.broadcast({"t": "aim", "pid": player.pid,
                                "angle": player.angle, "power": player.power,
                                "weapon": player.weapon}, skip=player.pid)

        elif kind == "weapon" and is_turn:
            if game.set_weapon(player, str(msg.get("code", ""))):
                self.broadcast({"t": "aim", "pid": player.pid,
                                "angle": player.angle, "power": player.power,
                                "weapon": player.weapon})

        elif kind == "move" and is_turn:
            steps = max(1, min(4, int(msg.get("steps", 1))))
            moved = False
            for _ in range(steps):
                moved |= game.move(player, int(msg.get("dir", 1)))
            if moved:
                self.broadcast({"t": "moved", "pid": player.pid,
                                "x": round(player.x, 1), "y": round(player.y, 1),
                                "fuel": player.fuel})

        elif kind == "shield" and is_turn:
            if game.activate_shield(player, str(msg.get("code", ""))):
                self.broadcast({"t": "shieldup", "pid": player.pid,
                                "sp": player.shield_hp})
                self.broadcast(game.state_wire())

        elif kind == "fire" and is_turn:
            self._do_fire(player)

        elif kind == "buy" and game.phase == PHASE_BUY:
            if game.buy(player, str(msg.get("code", "")), int(msg.get("qty", 1))):
                link.send({"t": "you", "player": player.to_wire()})
            else:
                link.send({"t": "error", "msg": "Cannot afford that"})

        elif kind == "sell" and game.phase == PHASE_BUY:
            if game.sell(player, str(msg.get("code", ""))):
                link.send({"t": "you", "player": player.to_wire()})

        elif kind == "buydone" and game.phase == PHASE_BUY:
            player.done_buying = True
            self.broadcast(game.state_wire())

        elif kind == "anim_done":
            link.acked_seq = max(link.acked_seq, int(msg.get("seq", 0)))

        elif kind == "chat":
            text = str(msg.get("text", ""))[:120].strip()
            if text:
                self.broadcast({"t": "chat", "pid": player.pid, "name": player.name,
                                "text": text})

    def _on_hello(self, link: Link, msg: dict) -> None:
        if link.hello:
            return
        version = int(msg.get("version", 0))
        if version != PROTOCOL_VERSION:
            link.send({"t": "error", "msg":
                       f"Version mismatch: server speaks {PROTOCOL_VERSION}, "
                       f"you speak {version}", "fatal": True})
            link.conn.close()
            return
        if self.game.phase != PHASE_LOBBY:
            link.send({"t": "error", "msg": "Match already in progress",
                       "fatal": True})
            link.conn.close()
            return
        player = self.game.add_player(str(msg.get("name", "Player")))
        if player is None:
            link.send({"t": "error", "msg": "Server is full", "fatal": True})
            link.conn.close()
            return
        link.pid = player.pid
        link.hello = True
        if self.host_pid is None:
            self.host_pid = player.pid
        link.send({
            "t": "welcome", "pid": player.pid, "host": link.pid == self.host_pid,
            "version": PROTOCOL_VERSION, "server": self.name,
            "settings": self.game.settings.to_wire(),
            "colors": list(COLOR_NAMES), "catalogue": W.catalogue(),
        })
        self._broadcast_lobby()
        self._log(f"{player.name} joined from {link.addr[0]}")

    def _set_color(self, player, colour: int) -> None:
        colour = max(0, min(MAX_PLAYERS - 1, colour))
        for other in self.game.players.values():
            if other is not player and other.color == colour:
                other.color = player.color        # swap rather than refuse
                break
        player.color = colour


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="scorched server", description="Run a headless Scorched server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--name", default="Scorched LAN")
    parser.add_argument("--bots", type=int, default=0)
    parser.add_argument("--skill", default="moderate", choices=sorted(SKILLS))
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--turn-time", type=int, default=45)
    parser.add_argument("--wind", type=int, default=60, help="maximum wind")
    parser.add_argument("--terrain", default="random")
    parser.add_argument("--walls", default="none", choices=("none", "rebound", "wrap"))
    parser.add_argument("--no-announce", action="store_true",
                        help="do not answer LAN discovery probes")
    args = parser.parse_args(argv)

    settings = Settings(rounds=args.rounds, turn_time=args.turn_time,
                        wind_max=args.wind, terrain_style=args.terrain,
                        wall_mode=args.walls)
    server = Server(port=args.port, name=args.name, settings=settings,
                    bots=args.bots, bot_skill=args.skill,
                    announce=not args.no_announce)
    server.start()
    print(f"[server] '{args.name}' listening on port {server.bound_port}", flush=True)
    print("[server] waiting for players (Ctrl-C to stop)", flush=True)
    try:
        server.run()
    except KeyboardInterrupt:
        print("\n[server] shutting down", flush=True)
    finally:
        server.stop()
    return 0
