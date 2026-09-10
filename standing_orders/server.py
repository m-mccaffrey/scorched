"""Authoritative server for Standing Orders.

Clients send intent -- a batch of orders -- and receive facts: a fogged
timeline of what the turn actually did. The server is the only thing that ever
simulates anything, which is what makes a Pi and a desktop safe to put in the
same match.

Runs headless (``python -m standing_orders server``) or on a background thread
inside the host player's own client, so hosting is a button rather than a
second terminal.
"""

from __future__ import annotations

import argparse
import random
import socket
import threading
import time

from lanlib.discovery import Beacon
from lanlib.protocol import PROTOCOL_VERSION, Connection
from lanlib.theme import COLOR_NAMES

from .ai import BotBrain, SKILLS
from .game import (PHASE_LOBBY, PHASE_ORDERS, PHASE_OVER, PHASE_RESOLVE,
                   Match, Settings, available_maps)
from .state import MAX_PLAYERS
from .units import catalogue

GAME_ID = "standing-orders"
SO_PORT = 27019
TICK_HZ = 20
HEARTBEAT = 0.5

#: Extra seconds allowed for the slowest client to finish watching a turn
#: before the match moves on without it.
REPLAY_GRACE = 4.0

#: Bots take a moment to "think" so a turn does not snap past the humans.
BOT_THINK = (0.4, 1.1)

BOT_NAMES = ("Ada", "Grace", "Turing", "Hopper", "Lovelace", "Babbage")


class Link:
    def __init__(self, conn: Connection, addr) -> None:
        self.conn = conn
        self.addr = addr
        self.pid: int | None = None
        self.hello = False
        self.acked = 0

    def send(self, msg: dict) -> None:
        self.conn.send(msg)


class Server:
    def __init__(self, port: int = SO_PORT, name: str = "Standing Orders",
                 settings: Settings | None = None, bots: int = 0,
                 bot_skill: str = "moderate", announce: bool = True,
                 seed: int | None = None) -> None:
        self.port = port
        self.name = (name or "Standing Orders")[:24]
        self.match = Match(settings)
        self.links: list[Link] = []
        self.host_pid: int | None = None
        self.running = threading.Event()
        self.beacon: Beacon | None = None
        self.bound_port = port
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._rng = random.Random(seed)
        self._brains: dict[int, BotBrain] = {}
        self._bot_at: dict[int, float] = {}
        self._replay_until = 0.0
        self._last_status = 0.0
        self._announce = announce
        self._pending_bots = (bots, bot_skill)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", self.port))
        self._sock.listen(16)
        self.bound_port = self._sock.getsockname()[1]
        self.running.set()
        threading.Thread(target=self._accept_loop, name="so-accept",
                         daemon=True).start()
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

    def run(self) -> None:
        period = 1.0 / TICK_HZ
        while self.running.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception as exc:
                print(f"[so] tick error: {exc!r}", flush=True)
            time.sleep(max(0.0, period - (time.monotonic() - started)))

    def _beacon_info(self) -> dict:
        players = self.match.state.players
        return {
            "game": GAME_ID, "name": self.name, "port": self.bound_port,
            "version": PROTOCOL_VERSION, "players": len(players),
            "humans": sum(1 for p in players.values() if not p.bot),
            "max": MAX_PLAYERS, "phase": self.match.phase,
            "map": self.match.settings.map_name,
        }

    # -- connections -------------------------------------------------------
    def _accept_loop(self) -> None:
        while self.running.is_set():
            try:
                sock, addr = self._sock.accept()
            except OSError:
                break
            with self._lock:
                self.links.append(Link(Connection(sock, name=str(addr)), addr))

    def _drop(self, link: Link) -> None:
        if link in self.links:
            self.links.remove(link)
        link.conn.close()
        pid = link.pid
        if pid is None:
            return
        player = self.match.state.players.get(pid)
        if player is None:
            return
        if self.match.phase == PHASE_LOBBY:
            self.match.remove_player(pid)
        else:
            # Mid-match: hand the army to the machine rather than leaving an
            # empty seat that never files orders.
            player.connected = False
            player.bot = True
            player.name = f"{player.name}*"
            self._brains[pid] = BotBrain(player.skill, self._rng)
            print(f"[so] {player.name} dropped; a bot takes over", flush=True)
        if self.host_pid == pid:
            self.host_pid = None
            self._promote_host()
        self._broadcast_lobby()

    def _promote_host(self) -> None:
        for link in self.links:
            player = self.match.state.players.get(link.pid) if link.pid else None
            if player is not None and not player.bot and player.connected:
                self.host_pid = link.pid
                link.send({"t": "host", "host": True})
                return

    def broadcast(self, msg: dict) -> None:
        for link in list(self.links):
            if link.hello:
                link.send(msg)

    def send_to(self, pid: int, msg: dict) -> None:
        for link in self.links:
            if link.pid == pid and link.hello:
                link.send(msg)
                return

    # -- main tick ---------------------------------------------------------
    def tick(self) -> None:
        self._pump()
        now = time.monotonic()
        match = self.match

        if match.phase == PHASE_ORDERS:
            self._tick_orders(now)
        elif match.phase == PHASE_RESOLVE:
            self._tick_replay(now)

        if now - self._last_status >= HEARTBEAT:
            self._last_status = now
            self.broadcast({"t": "status", **match.status_wire()})

    def _pump(self) -> None:
        with self._lock:
            links = list(self.links)
        for link in links:
            for msg in link.conn.poll():
                if msg.get("t") == "__closed__":
                    with self._lock:
                        self._drop(link)
                    break
                try:
                    self._handle(link, msg)
                except Exception as exc:
                    print(f"[so] bad message from {link.addr}: {exc!r}", flush=True)

    def _tick_orders(self, now: float) -> None:
        match = self.match
        for player in match.state.players.values():
            if not (player.bot and player.alive and not player.ready):
                continue
            due = self._bot_at.get(player.pid)
            if due is None:
                lo, hi = BOT_THINK
                self._bot_at[player.pid] = now + self._rng.uniform(lo, hi)
            elif now >= due:
                self._bot_at.pop(player.pid, None)
                self._run_bot(player)

        expired = match.deadline > 0 and now >= match.deadline
        if match.everyone_ready() or expired:
            self._resolve()

    def _run_bot(self, player) -> None:
        brain = self._brains.setdefault(player.pid,
                                        BotBrain(player.skill, self._rng))
        try:
            orders = brain.plan(self.match, player)
        except Exception as exc:
            print(f"[so] bot {player.name} failed to think: {exc!r}", flush=True)
            orders = []
        self.match.submit(player.pid, orders)

    def _resolve(self) -> None:
        timelines = self.match.resolve()
        for pid, payload in timelines.items():
            self.send_to(pid, {"t": "turn", **payload})
        for link in self.links:
            link.acked = max(link.acked, self.match.turn_seq - 1)
        longest = max((len(p["events"]) for p in timelines.values()), default=0)
        self._replay_until = (time.monotonic() + REPLAY_GRACE
                              + min(12.0, longest * 0.02) + 2.0)
        self._bot_at.clear()
        self._flush_log()

    def _tick_replay(self, now: float) -> None:
        seq = self.match.turn_seq
        waiting = [l for l in self.links
                   if l.hello and l.pid is not None and l.acked < seq]
        if waiting and now < self._replay_until:
            return
        if self.match.begin_orders():
            self.broadcast({"t": "orders_open", **self.match.status_wire()})
        else:
            self.broadcast({"t": "over", "standings": self.match.standings(),
                            "winner": self.match.winner_team})
        self._flush_log()

    def _flush_log(self) -> None:
        if self.match.log:
            self.broadcast({"t": "log", "lines": self.match.log[-5:]})

    def _broadcast_lobby(self) -> None:
        wire = self.match.lobby_wire()
        wire["host"] = self.host_pid if self.host_pid is not None else -1
        self.broadcast({"t": "lobby", **wire})

    # -- bots --------------------------------------------------------------
    def add_bot(self, skill: str = "moderate") -> bool:
        if self.match.phase != PHASE_LOBBY:
            return False
        used = {p.name for p in self.match.state.players.values()}
        name = next((n for n in BOT_NAMES if n not in used), "Bot")
        skill = skill if skill in SKILLS else "moderate"
        player = self.match.add_player(name, bot=True, skill=skill)
        if player is None:
            return False
        self._brains[player.pid] = BotBrain(skill, self._rng)
        self._broadcast_lobby()
        return True

    # -- messages ----------------------------------------------------------
    def _handle(self, link: Link, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "hello":
            self._on_hello(link, msg)
            return
        if not link.hello or link.pid is None:
            return
        match = self.match
        player = match.state.players.get(link.pid)
        if player is None:
            return
        is_host = link.pid == self.host_pid

        if kind == "ping":
            link.send({"t": "pong", "ts": msg.get("ts")})

        elif kind == "setup" and match.phase == PHASE_LOBBY:
            if "name" in msg:
                player.name = str(msg["name"])[:14] or player.name
            if "color" in msg:
                self._set_color(player, int(msg["color"]))
            if "ready" in msg:
                player.ready = bool(msg["ready"])
            self._broadcast_lobby()

        elif kind == "settings" and is_host and match.phase == PHASE_LOBBY:
            wanted = Settings.from_wire(msg.get("settings", {}))
            if wanted.map_name not in available_maps():
                wanted.map_name = match.settings.map_name
            match.settings = wanted
            match.set_teams(wanted.teams)
            self._broadcast_lobby()

        elif kind == "addbot" and is_host:
            self.add_bot(str(msg.get("skill", "moderate")))

        elif kind == "kick" and is_host and match.phase == PHASE_LOBBY:
            target = match.state.players.get(int(msg.get("pid", -1)))
            if target is not None and target.bot:
                match.remove_player(target.pid)
                self._brains.pop(target.pid, None)
                self._broadcast_lobby()

        elif kind == "start" and is_host and match.phase == PHASE_LOBBY:
            if len(match.state.players) >= 2:
                match.start_match()
                self._broadcast_start()
            else:
                link.send({"t": "error", "msg": "Need at least two commanders"})

        elif kind == "orders" and match.phase == PHASE_ORDERS:
            orders = msg.get("orders", [])
            if isinstance(orders, list):
                match.submit(link.pid, orders)
                self.broadcast({"t": "status", **match.status_wire()})

        elif kind == "unready" and match.phase == PHASE_ORDERS:
            match.unready(link.pid)
            self.broadcast({"t": "status", **match.status_wire()})

        elif kind == "replay_done":
            link.acked = max(link.acked, int(msg.get("seq", 0)))

        elif kind == "restart" and is_host and match.phase == PHASE_OVER:
            match.phase = PHASE_LOBBY
            for other in match.state.players.values():
                other.ready = other.bot
                other.alive = True
            self._broadcast_lobby()

        elif kind == "chat":
            text = str(msg.get("text", ""))[:120].strip()
            if text:
                self.broadcast({"t": "chat", "pid": link.pid,
                                "name": player.name, "text": text})

    def _broadcast_start(self) -> None:
        wire = self.match.state.map.to_wire()
        for player in self.match.state.players.values():
            self.send_to(player.pid, {
                "t": "start", "map": wire, "you": player.pid,
                "settings": self.match.settings.to_wire(),
                "players": [p.to_wire() for p in self.match.state.players.values()],
            })
        # Turn one arrives as a normal opening, so the client only has one
        # code path for "here is the board, file your orders".
        self._send_initial_views()
        self.broadcast({"t": "orders_open", **self.match.status_wire()})
        self._flush_log()

    def _send_initial_views(self) -> None:
        from .fog import VisionCache, team_vision, visible_state
        cache = VisionCache(self.match.state.map)
        for player in self.match.state.players.values():
            vision = team_vision(self.match.state, player.team, cache)
            self.send_to(player.pid, {
                "t": "view",
                "state": visible_state(self.match.state, player.pid, vision),
            })

    def _on_hello(self, link: Link, msg: dict) -> None:
        if link.hello:
            return
        if int(msg.get("version", 0)) != PROTOCOL_VERSION:
            link.send({"t": "error", "fatal": True, "msg":
                       f"Version mismatch: server speaks {PROTOCOL_VERSION}"})
            link.conn.close()
            return
        if msg.get("game") not in (None, GAME_ID):
            link.send({"t": "error", "fatal": True,
                       "msg": "That is a different game"})
            link.conn.close()
            return
        if self.match.phase != PHASE_LOBBY:
            link.send({"t": "error", "fatal": True,
                       "msg": "Match already in progress"})
            link.conn.close()
            return
        player = self.match.add_player(str(msg.get("name", "Commander")))
        if player is None:
            link.send({"t": "error", "fatal": True, "msg": "Server is full"})
            link.conn.close()
            return
        link.pid = player.pid
        link.hello = True
        if self.host_pid is None:
            self.host_pid = player.pid
        link.send({
            "t": "welcome", "pid": player.pid, "game": GAME_ID,
            "host": link.pid == self.host_pid, "server": self.name,
            "version": PROTOCOL_VERSION, "colors": list(COLOR_NAMES),
            "catalogue": catalogue(), "maps": available_maps(),
            "settings": self.match.settings.to_wire(),
        })
        self._broadcast_lobby()
        print(f"[so] {player.name} joined from {link.addr[0]}", flush=True)

    def _set_color(self, player, colour: int) -> None:
        colour = max(0, min(MAX_PLAYERS - 1, colour))
        for other in self.match.state.players.values():
            if other is not player and other.color == colour:
                other.color = player.color
                break
        player.color = colour


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="standing_orders server",
        description="Run a headless Standing Orders server")
    parser.add_argument("--port", type=int, default=SO_PORT)
    parser.add_argument("--name", default="Standing Orders LAN")
    parser.add_argument("--bots", type=int, default=0)
    parser.add_argument("--skill", default="moderate", choices=sorted(SKILLS))
    parser.add_argument("--map", default="duel", choices=available_maps())
    parser.add_argument("--teams", action="store_true", help="2v2 with four players")
    parser.add_argument("--order-time", type=int, default=90)
    parser.add_argument("--no-announce", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings(map_name=args.map, teams=args.teams,
                        order_time=args.order_time)
    server = Server(port=args.port, name=args.name, settings=settings,
                    bots=args.bots, bot_skill=args.skill,
                    announce=not args.no_announce)
    server.start()
    print(f"[so] '{args.name}' listening on port {server.bound_port}", flush=True)
    print("[so] waiting for commanders (Ctrl-C to stop)", flush=True)
    try:
        server.run()
    except KeyboardInterrupt:
        print("\n[so] shutting down", flush=True)
    finally:
        server.stop()
    return 0
