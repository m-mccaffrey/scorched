"""The Standing Orders client.

One window, one state machine. Hosting spawns the authoritative server on a
background thread in this same process, so "Host Game" is a button rather than
a second terminal.

The order phase is the whole interface: click units, right-click where they
should go, queue production, press Ready. Nothing is timed to your reflexes,
so the mouse handling can be simple and forgiving.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pygame

from lanlib import discovery, ui
from lanlib.protocol import PROTOCOL_VERSION, Connection, connect
from lanlib.theme import (UI_ACCENT, UI_BG, UI_DIM, UI_GOOD,
                          UI_PANEL, UI_PANEL_HI, UI_PANEL_LO, UI_TEXT, UI_WARN,
                          shade, team_color)

from ..game import Settings, available_maps
from ..grid import TileMap, describe, find_path
from ..server import GAME_ID, SO_PORT, Server
from ..state import MAX_PLAYERS
from ..units import (BUILDING, BUILDINGS, HARVEST_RADIUS, HARVEST_RATE,
                     RESEARCH_BY_CODE, UNIT, available_research,
                     cost_of_building, catalogue)
from .audio import OrdersSfx
from .render import (HUD_H, PANEL_W, SCREEN_H, SCREEN_W, TOP_H, Renderer,
                     draw_tooltip)
from .replay import ReplayPlayer
from .view import WorldView

FPS = 60
SKILLS = ("novice", "moderate", "veteran", "cyborg")


class App:
    def __init__(self, name: str = "", fullscreen: bool = False,
                 sound: bool = True, scaled: bool = True) -> None:
        pygame.init()
        # Caches from a previously closed game hold dead Font objects.
        ui.reset()
        pygame.display.set_caption("Standing Orders - LAN Skirmish")
        flags = pygame.RESIZABLE | (pygame.SCALED if scaled else 0)
        if fullscreen:
            flags |= pygame.FULLSCREEN
        self.screen = self._open_display(flags)
        self.clock = pygame.time.Clock()
        self.sfx = OrdersSfx(enabled=sound)
        self.renderer = Renderer()
        self.view = WorldView()
        self.running = True
        self.mode = "menu"

        self.conn: Connection | None = None
        self.server: Server | None = None
        self.player_name = name or _default_name()
        self.my_pid = -1
        self.is_host = False
        self.server_name = ""
        self.error = ""
        self.error_until = 0.0
        self.status = ""

        self.players: dict[int, dict] = {}
        self.colors: dict[int, int] = {}
        self.settings = Settings()
        self.maps = available_maps()
        self.catalogue = catalogue()
        self.phase = "lobby"
        self.turn = 0
        self.time_left = -1.0
        self.log: list[str] = []
        self.chat: list = []
        self.standings: list = []
        self.winner_team: int | None = None

        # Order-phase working state
        self.selected: set = set()
        self.unit_orders: dict[int, dict] = {}
        self.queued: list = []            # train / build orders
        self.placing: str | None = None   # building code awaiting a site
        self.selected_building: int | None = None
        self.drag_anchor: tuple | None = None
        self.ready_sent = False
        self.rejected: list = []

        self.replay: ReplayPlayer | None = None
        self.mouse = (0, 0)
        self.chat_input: ui.TextInput | None = None
        self._buttons: list = []
        self._servers: list = []
        self._scanning = False
        self._addr = ui.TextInput((0, 0, 200, 22), "", 40)
        self._name = ui.TextInput((0, 0, 200, 22), self.player_name, 14)
        self._bots = 1
        self._skill = "moderate"
        #: (rect, lines) pairs collected while drawing; whichever the pointer
        #: is over gets a tooltip once the frame is otherwise finished.
        self._tips: list = []

    @staticmethod
    def _open_display(flags: int) -> pygame.Surface:
        for attempt, vsync in ((flags, 1), (flags, 0),
                               (flags & ~pygame.SCALED, 0)):
            try:
                return pygame.display.set_mode((SCREEN_W, SCREEN_H), attempt,
                                               vsync=vsync)
            except pygame.error:
                continue
        return pygame.display.set_mode((SCREEN_W, SCREEN_H))

    # =====================================================================
    # loop
    # =====================================================================
    def run(self) -> None:
        while self.running:
            dt = self.clock.tick(FPS) / 1000.0
            self.mouse = pygame.mouse.get_pos()
            self._events()
            self._pump()
            self._update(dt)
            self._draw()
            pygame.display.flip()
        self._teardown()

    def _teardown(self) -> None:
        if self.conn:
            self.conn.close()
        if self.server:
            self.server.stop()
        pygame.quit()

    def _update(self, dt: float) -> None:
        if self.replay is not None:
            if not self.replay.update(dt):
                self._finish_replay()
        if self.time_left > 0:
            self.time_left = max(0.0, self.time_left - dt)

    def _finish_replay(self) -> None:
        payload_state = self.replay.final_state
        self.send({"t": "replay_done", "seq": self.replay.seq})
        self.replay = None
        if payload_state:
            self.view.apply_state(payload_state)
            self.renderer.set_fog(self.view.visible, self.view.explored)
        self._clear_orders()

    def _clear_orders(self) -> None:
        self.selected.clear()
        self.unit_orders.clear()
        self.queued.clear()
        self.placing = None
        self.selected_building = None
        self.ready_sent = False

    # =====================================================================
    # networking
    # =====================================================================
    def host_game(self, settings: Settings, bots: int, skill: str) -> None:
        self.disconnect()
        try:
            self.server = Server(port=SO_PORT, name=f"{self.player_name}'s war",
                                 settings=settings, bots=bots, bot_skill=skill)
            self.server.start()
        except OSError as exc:
            self._complain(f"Could not host: {exc}")
            self.server = None
            return
        threading.Thread(target=self.server.run, name="so-server",
                         daemon=True).start()
        self.join_game("127.0.0.1", self.server.bound_port)

    def join_game(self, host: str, port: int = SO_PORT) -> None:
        self.error = ""
        self.status = f"Connecting to {host}..."
        self._draw()
        pygame.display.flip()
        try:
            self.conn = connect(host, port, timeout=6.0)
        except (ConnectionError, OSError, socket.gaierror) as exc:
            self._complain(f"Could not connect: {exc}")
            self.status = ""
            return
        self.conn.send({"t": "hello", "name": self.player_name,
                        "game": GAME_ID, "version": PROTOCOL_VERSION})
        self.status = ""
        self.mode = "lobby"

    def disconnect(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None
        if self.server:
            self.server.stop()
            self.server = None
        self.replay = None
        self.view = WorldView()
        self.players.clear()
        self._clear_orders()

    def _complain(self, text: str, seconds: float = 4.0) -> None:
        self.error = text
        self.error_until = time.monotonic() + seconds

    def send(self, msg: dict) -> None:
        if self.conn:
            self.conn.send(msg)

    def _pump(self) -> None:
        if not self.conn:
            return
        for msg in self.conn.poll():
            try:
                self._on_message(msg)
            except Exception as exc:
                self.log.append(f"bad message: {exc!r}")

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("t")

        if kind == "__closed__":
            self._complain(f"Disconnected: {msg.get('reason', 'connection lost')}")
            self.disconnect()
            self.mode = "menu"

        elif kind == "welcome":
            self.my_pid = msg["pid"]
            self.is_host = msg.get("host", False)
            self.server_name = msg.get("server", "")
            self.catalogue = msg.get("catalogue", self.catalogue)
            self.maps = msg.get("maps", self.maps)
            self.settings = Settings.from_wire(msg.get("settings", {}))

        elif kind == "host":
            self.is_host = msg.get("host", False)

        elif kind == "lobby":
            self._sync_players(msg.get("players", []))
            self.settings = Settings.from_wire(msg.get("settings", {}))
            self.maps = msg.get("maps", self.maps)
            self.is_host = msg.get("host", -1) == self.my_pid
            if msg.get("phase") == "lobby":
                self.mode = "lobby"

        elif kind == "start":
            tilemap = TileMap.from_wire(msg["map"])
            self.renderer.begin_match(tilemap)
            self._sync_players(msg.get("players", []))
            self.settings = Settings.from_wire(msg.get("settings", {}))
            self.view = WorldView()
            self.mode = "game"
            self.winner_team = None
            self._clear_orders()

        elif kind == "view":
            self.view.apply_state(msg["state"])
            self.renderer.set_fog(self.view.visible, self.view.explored)

        elif kind == "status":
            self.phase = msg.get("phase", self.phase)
            self.turn = msg.get("turn", self.turn)
            self.time_left = msg.get("time", -1)
            self._sync_players(msg.get("players", []))
            self.standings = msg.get("standings", self.standings)

        elif kind == "orders_open":
            self.phase = "orders"
            self.turn = msg.get("turn", self.turn)
            self.time_left = msg.get("time", -1)
            self._sync_players(msg.get("players", []))
            self.mode = "game"
            self._clear_orders()
            self.sfx.play("turn")

        elif kind == "turn":
            self.phase = "resolve"
            self.rejected = msg.get("rejected", [])
            if self.rejected:
                self.sfx.play("deny")
            self.replay = ReplayPlayer(msg, self.view, self.renderer,
                                       self.colors, self.sfx)

        elif kind == "over":
            self.standings = msg.get("standings", [])
            self.winner_team = msg.get("winner")
            self.phase = "over"
            self.mode = "over"
            self.sfx.play("win")

        elif kind == "log":
            for line in msg.get("lines", []):
                if line not in self.log:
                    self.log.append(line)
            del self.log[:-8]

        elif kind == "chat":
            colour = team_color(self.colors.get(msg.get("pid", 0), 0))
            self.chat.append((msg.get("name", "?"), msg.get("text", ""), colour))
            del self.chat[:-5]

        elif kind == "error":
            self._complain(msg.get("msg", "error"))
            self.sfx.play("deny")
            if msg.get("fatal"):
                self.disconnect()
                self.mode = "menu"

    def _sync_players(self, rows: list) -> None:
        for row in rows:
            self.players[row["pid"]] = row
            self.colors[row["pid"]] = row.get("color", 0)
        live = {row["pid"] for row in rows}
        if live:
            for pid in [p for p in self.players if p not in live]:
                del self.players[pid]

    # =====================================================================
    # input
    # =====================================================================
    def _events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
                return
            if event.type == pygame.KEYDOWN and event.key == pygame.K_F11:
                pygame.display.toggle_fullscreen()
                continue
            if self.chat_input is not None:
                self._chat_event(event)
                continue
            handler = getattr(self, f"_events_{self.mode}", None)
            if handler:
                handler(event)

    def _chat_event(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.chat_input = None
            return
        if self.chat_input.handle(event):
            text = self.chat_input.value.strip()
            if text:
                self.send({"t": "chat", "text": text})
            self.chat_input = None

    def _open_chat(self) -> None:
        self.chat_input = ui.TextInput((60, SCREEN_H - 60, 500, 22), "", 100)
        self.chat_input.focused = True

    # -- front end ---------------------------------------------------------
    def _events_menu(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.running = False
        self._name.handle(event)
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    self.player_name = (self._name.value.strip() or "Commander")[:14]
                    if button.action == "host":
                        self.mode = "hostsetup"
                    elif button.action == "browse":
                        self.mode = "browse"
                        self._start_scan()
                    elif button.action == "direct":
                        self.mode = "direct"
                        self._addr.focused = True
                    elif button.action == "quit":
                        self.running = False

    def _events_hostsetup(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.mode = "menu"
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    self._host_action(button.action)

    def _host_action(self, action: str) -> None:
        if action == "back":
            self.mode = "menu"
        elif action == "go":
            self.host_game(self.settings, self._bots, self._skill)
        elif action.startswith("bots"):
            step = 1 if action.endswith("+") else -1
            self._bots = max(0, min(MAX_PLAYERS - 1, self._bots + step))
        elif action.startswith("skill"):
            step = 1 if action.endswith("+") else -1
            self._skill = SKILLS[(SKILLS.index(self._skill) + step) % len(SKILLS)]
        else:
            self._tweak(action)

    def _tweak(self, action: str) -> None:
        if ":" not in action:
            return
        field, direction = action.split(":")
        step = 1 if direction == "+" else -1
        settings = self.settings
        if field == "map":
            maps = self.maps or ["duel"]
            index = maps.index(settings.map_name) if settings.map_name in maps else 0
            settings.map_name = maps[(index + step) % len(maps)]
        elif field == "teams":
            settings.teams = not settings.teams
        elif field == "supply":
            settings.start_supply = max(0, min(60, settings.start_supply + step * 5))
        elif field == "clock":
            settings.order_time = max(0, min(300, settings.order_time + step * 15))
        settings.clamp()

    def _events_browse(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.mode = "menu"
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    if button.action == "back":
                        self.mode = "menu"
                    elif button.action == "rescan":
                        self._start_scan()
                    elif button.action.startswith("join:"):
                        _, host, port = button.action.split(":")
                        self.join_game(host, int(port))

    def _start_scan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self._servers = []

        def work():
            try:
                found = discovery.scan(1.4)
                # The beacon port is shared across the whole collection, so
                # filter to servers actually running this game.
                self._servers = [s for s in found if s.get("game") == GAME_ID]
            except OSError:
                self._servers = []
            finally:
                self._scanning = False

        threading.Thread(target=work, name="so-scan", daemon=True).start()

    def _events_direct(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.mode = "menu"
            return
        if self._addr.handle(event):
            self._connect_typed()
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    if button.action == "back":
                        self.mode = "menu"
                    elif button.action == "connect":
                        self._connect_typed()

    def _connect_typed(self) -> None:
        text = self._addr.value.strip()
        if not text:
            return
        host, _, port = text.partition(":")
        self.join_game(host.strip(), int(port) if port.isdigit() else SO_PORT)

    def _events_lobby(self, event) -> None:
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                self.disconnect()
                self.mode = "menu"
                return
            if event.key in (pygame.K_t, pygame.K_RETURN):
                self._open_chat()
                return
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    self._lobby_action(button.action)

    def _lobby_action(self, action: str) -> None:
        me = self.players.get(self.my_pid, {})
        if action == "leave":
            self.disconnect()
            self.mode = "menu"
        elif action == "ready":
            self.send({"t": "setup", "ready": not me.get("ready", False)})
        elif action == "color":
            self.send({"t": "setup",
                       "color": (me.get("color", 0) + 1) % MAX_PLAYERS})
        elif action == "addbot":
            self.send({"t": "addbot", "skill": self._skill})
        elif action.startswith("kick:"):
            self.send({"t": "kick", "pid": int(action.split(":")[1])})
        elif action == "start":
            self.send({"t": "start"})
        elif self.is_host:
            self._tweak(action)
            self.send({"t": "settings", "settings": self.settings.to_wire()})

    def _events_over(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.disconnect()
            self.mode = "menu"
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    if button.action == "again":
                        self.send({"t": "restart"})
                    elif button.action == "leave":
                        self.disconnect()
                        self.mode = "menu"

    # -- the order phase ---------------------------------------------------
    def _events_game(self, event) -> None:
        if event.type == pygame.KEYDOWN:
            self._game_key(event)
            return
        if self.phase != "orders" or self.replay is not None:
            return
        if event.type == pygame.MOUSEBUTTONDOWN:
            self._game_click(event)
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self._finish_drag()

    def _game_key(self, event) -> None:
        key = event.key
        if key == pygame.K_ESCAPE:
            if self.placing:
                self.placing = None
            elif self.selected or self.selected_building is not None:
                self.selected.clear()
                self.selected_building = None
            else:
                self.mode = "pause"
            return
        if key == pygame.K_t:
            self._open_chat()
        elif key == pygame.K_SPACE and self.replay is not None:
            self.replay.skip()
        elif key == pygame.K_RETURN and self.phase == "orders":
            self._send_ready()
        elif key == pygame.K_a and self.selected:
            self.status = "Attack-move: right-click a target"
        elif key == pygame.K_TAB:
            self._select_all_units()

    def _select_all_units(self) -> None:
        self.selected = {u["uid"] for u in self.view.mine(self.my_pid)}
        self.selected_building = None

    def _game_click(self, event) -> None:
        board = self.renderer.board
        if board is None:
            return
        tile = board.to_tile(event.pos)

        if event.button == 1:
            if tile is not None and self.placing:
                self._place_building(tile)
                return
            if tile is not None:
                self._select_at(tile, additive=bool(
                    pygame.key.get_mods() & pygame.KMOD_SHIFT))
                self.drag_anchor = event.pos
                return
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    self._panel_action(button.action)
                    return
        elif event.button == 3 and tile is not None:
            attack = bool(pygame.key.get_mods() &
                          (pygame.KMOD_SHIFT | pygame.KMOD_ALT))
            self._issue_move(tile, attack)

    def _select_at(self, tile, additive: bool) -> None:
        unit = self.view.unit_at(tile)
        if unit is not None and unit["owner"] == self.my_pid:
            if not additive:
                self.selected.clear()
            self.selected.add(unit["uid"])
            self.selected_building = None
            self.sfx.play("select")
            return
        building = self.view.building_at(tile)
        if building is not None and building["owner"] == self.my_pid:
            self.selected.clear()
            self.selected_building = building["bid"]
            self.sfx.play("select")
            return
        if not additive:
            self.selected.clear()
            self.selected_building = None

    def _finish_drag(self) -> None:
        anchor, self.drag_anchor = self.drag_anchor, None
        if anchor is None:
            return
        if abs(anchor[0] - self.mouse[0]) < 5 and abs(anchor[1] - self.mouse[1]) < 5:
            return
        board = self.renderer.board
        rect = pygame.Rect(min(anchor[0], self.mouse[0]),
                           min(anchor[1], self.mouse[1]),
                           abs(self.mouse[0] - anchor[0]),
                           abs(self.mouse[1] - anchor[1]))
        picked = set()
        for unit in self.view.mine(self.my_pid):
            if rect.collidepoint(board.centre((unit["x"], unit["y"]))):
                picked.add(unit["uid"])
        if picked:
            self.selected = picked
            self.selected_building = None
            self.sfx.play("select")

    def _issue_move(self, tile, attack: bool) -> None:
        """Send the selection somewhere, fanning out so they do not queue up.

        Ordering six units onto one tile means five of them get blocked and
        stop, which looks broken. Spreading them over nearby free tiles is
        what the player meant anyway.
        """
        if not self.selected:
            return
        board = self.renderer.board
        blocked = {(u["x"], u["y"]) for u in self.view.units.values()}
        blocked |= {(b["x"], b["y"]) for b in self.view.buildings.values()}
        targets = self._formation(tile, len(self.selected), blocked)
        units = sorted(
            (self.view.units[uid] for uid in self.selected if uid in self.view.units),
            key=lambda u: abs(u["x"] - tile[0]) + abs(u["y"] - tile[1]))
        for unit, goal in zip(units, targets):
            path = find_path(board.map, (unit["x"], unit["y"]), goal,
                             blocked - {(unit["x"], unit["y"]), goal})
            self.unit_orders[unit["uid"]] = {
                "o": "attack" if attack else "move", "uid": unit["uid"],
                "to": list(goal), "path": path}
        self.sfx.play("order")
        self.ready_sent = False

    def _formation(self, centre, count: int, blocked: set) -> list:
        """``count`` distinct passable tiles clustered on ``centre``."""
        tilemap = self.renderer.board.map
        out = []
        radius = 0
        while len(out) < count and radius < 6:
            ring = []
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    tile = (centre[0] + dx, centre[1] + dy)
                    if tilemap.passable(*tile) and tile not in out:
                        ring.append(tile)
            ring.sort(key=lambda t: (abs(t[0] - centre[0]) + abs(t[1] - centre[1]), t))
            out.extend(ring)
            radius += 1
        return out[:count] or [centre]

    def _panel_action(self, action: str) -> None:
        if action == "ready":
            self._send_ready()
        elif action == "unready":
            self.send({"t": "unready"})
            self.ready_sent = False
        elif action == "clear":
            self._clear_orders()
        elif action.startswith("train:"):
            self._queue_train(action.split(":")[1])
        elif action.startswith("place:"):
            self.placing = action.split(":")[1]
        elif action.startswith("research:"):
            self._queue_research(action.split(":")[1])
        elif action == "skip" and self.replay is not None:
            self.replay.skip()

    def _queue_train(self, code: str) -> None:
        if self.selected_building is None:
            return
        building = self.view.buildings.get(self.selected_building)
        if building is None:
            return
        if self._spent() + UNIT[code].cost > self.view.supply:
            self._complain("Not enough supply")
            self.sfx.play("deny")
            return
        cap = self._army_cap()
        if self._army_size() >= cap:
            self._complain(f"Army capped at {cap} -- build a Supply Depot")
            self.sfx.play("deny")
            return
        self.queued.append({"o": "train", "bid": building["bid"], "code": code})
        self.sfx.play("order")
        self.ready_sent = False

    def _queue_research(self, code: str) -> None:
        base = next((b for b in self.view.buildings.values()
                     if b["owner"] == self.my_pid and b["code"] == "base"), None)
        project = RESEARCH_BY_CODE.get(code)
        if base is None or project is None:
            return
        if base.get("project") or any(o["o"] == "research" for o in self.queued):
            self._complain("Already researching something")
            self.sfx.play("deny")
            return
        if self._spent() + project.cost > self.view.supply:
            self._complain("Not enough supply")
            self.sfx.play("deny")
            return
        self.queued.append({"o": "research", "bid": base["bid"], "code": code})
        self.sfx.play("order")
        self.ready_sent = False

    def _place_building(self, tile) -> None:
        """Assign the job to an Engineer, who walks there and builds it."""
        code, self.placing = self.placing, None
        workers = [u for u in self.view.mine(self.my_pid)
                   if UNIT[u["code"]].builder]
        if not workers:
            self._complain("You need an Engineer to build")
            self.sfx.play("deny")
            return
        price = cost_of_building(code, self._my_research())
        if self._spent() + price > self.view.supply:
            self._complain("Not enough supply")
            self.sfx.play("deny")
            return
        if self.view.building_at(tile) or self.view.unit_at(tile):
            self._complain("That tile is occupied")
            self.sfx.play("deny")
            return
        # Prefer an Engineer the player has selected; otherwise the nearest.
        chosen = next((u for u in workers if u["uid"] in self.selected), None)
        if chosen is None:
            chosen = min(workers, key=lambda u: abs(u["x"] - tile[0])
                         + abs(u["y"] - tile[1]))
        self.queued.append({"o": "build", "uid": chosen["uid"], "code": code,
                            "to": list(tile)})
        self.sfx.play("order")
        self.ready_sent = False

    def _spent(self) -> int:
        done = self._my_research()
        total = 0
        for order in self.queued:
            if order["o"] == "train":
                total += UNIT[order["code"]].cost
            elif order["o"] == "build":
                total += cost_of_building(order["code"], done)
            elif order["o"] == "research":
                project = RESEARCH_BY_CODE.get(order["code"])
                total += project.cost if project else 0
        return total

    def _army_size(self) -> int:
        """What the server counts, plus anything queued but not yet sent."""
        return self.view.army + sum(1 for o in self.queued if o["o"] == "train")

    def _army_cap(self) -> int:
        cap = self.view.cap
        # A depot we have queued this turn does not count until it is built,
        # so this is deliberately the server's number and not a prediction.
        return cap if cap else 0

    def _my_research(self) -> set:
        return set(self.players.get(self.my_pid, {}).get("research", []))

    def _send_ready(self) -> None:
        orders = [{k: v for k, v in order.items() if k != "path"}
                  for order in self.unit_orders.values()]
        orders.extend(self.queued)
        self.send({"t": "orders", "orders": orders})
        self.ready_sent = True
        self.sfx.play("ready")

    def _events_pause(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.mode = "game"
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    if button.action == "resume":
                        self.mode = "game"
                    elif button.action == "leave":
                        self.disconnect()
                        self.mode = "menu"

    # =====================================================================
    # drawing
    # =====================================================================
    def _draw(self) -> None:
        self._buttons = []
        self._tips = []
        painter = getattr(self, f"_draw_{self.mode}", self._draw_menu)
        painter()
        if self.chat_input is not None:
            ui.draw_text(self.screen, "SAY:", 26, SCREEN_H - 56, 16, UI_ACCENT)
            self.chat_input.draw(self.screen)
        for button in self._buttons:
            button.update(self.mouse)
            button.draw(self.screen)
        for rect, lines in self._tips:
            if rect.collidepoint(self.mouse):
                draw_tooltip(self.screen, lines, self.mouse)
                break
        if self.error and time.monotonic() < self.error_until:
            y = SCREEN_H - HUD_H - 14 if self.mode in ("game", "pause") else SCREEN_H - 14
            ui.draw_text(self.screen, self.error, SCREEN_W // 2, y, 15, UI_WARN,
                         anchor="center", shadow=True)
        elif self.error:
            self.error = ""

    def _button(self, rect, label, action, size=17, enabled=True, hidden=False):
        button = ui.Button(rect, label, action, size, enabled, hidden)
        self._buttons.append(button)
        return button

    # -- front end ---------------------------------------------------------
    def _backdrop(self) -> None:
        self.screen.fill(UI_BG)
        for y in range(0, SCREEN_H, 4):
            self.screen.fill((22, 24, 32), (0, y, SCREEN_W, 1))

    def _draw_menu(self) -> None:
        self._backdrop()
        ui.draw_text(self.screen, "STANDING ORDERS", SCREEN_W // 2 + 2, 48, 46,
                     (10, 10, 14), anchor="center")
        ui.draw_text(self.screen, "STANDING ORDERS", SCREEN_W // 2, 46, 46,
                     UI_ACCENT, anchor="center")
        ui.draw_text(self.screen, "PLAN IN SECRET  ·  WATCH IT ALL HAPPEN AT ONCE",
                     SCREEN_W // 2, 84, 16, UI_DIM, anchor="center")
        self._name.rect = pygame.Rect(SCREEN_W // 2 - 100, 124, 200, 22)
        self._name.draw(self.screen, "COMMANDER")
        y = 172
        for label, action in (("Host a Game", "host"),
                              ("Find LAN Games", "browse"),
                              ("Connect by Address", "direct"),
                              ("Quit", "quit")):
            self._button((SCREEN_W // 2 - 100, y, 200, 28), label, action)
            y += 36
        ui.draw_text(self.screen, "F11 fullscreen  ·  right-click to order  ·  Enter to commit",
                     SCREEN_W // 2, SCREEN_H - 26, 14, UI_DIM, anchor="center")
        if self.status:
            ui.draw_text(self.screen, self.status, SCREEN_W // 2, SCREEN_H - 46,
                         16, UI_TEXT, anchor="center")

    def _draw_hostsetup(self) -> None:
        self._backdrop()
        ui.draw_text(self.screen, "HOST A GAME", SCREEN_W // 2, 16, 30,
                     UI_ACCENT, anchor="center")
        panel = pygame.Rect(110, 52, SCREEN_W - 220, 200)
        ui.draw_panel(self.screen, panel)
        rows = [("Map", self.settings.map_name.title(), "map"),
                ("Teams (4p)", "2v2" if self.settings.teams else "free-for-all", "teams"),
                ("Starting supply", str(self.settings.start_supply), "supply"),
                ("Order clock", f"{self.settings.order_time}s"
                 if self.settings.order_time else "off", "clock"),
                ("Computer players", str(self._bots), "bots"),
                ("Bot skill", self._skill.title(), "skill")]
        y = panel.y + 12
        for label, value, field in rows:
            ui.draw_text(self.screen, label, panel.x + 14, y + 3, 17, UI_TEXT)
            self._button((panel.right - 120, y, 20, 20), "<", f"{field}:-", 17)
            ui.draw_text(self.screen, value, panel.right - 68, y + 3, 16,
                         UI_ACCENT, anchor="midtop")
            self._button((panel.right - 34, y, 20, 20), ">", f"{field}:+", 17)
            y += 30
        self._button((SCREEN_W // 2 - 150, 268, 140, 28), "Back", "back")
        self._button((SCREEN_W // 2 + 10, 268, 140, 28), "Start Hosting", "go")

    def _draw_browse(self) -> None:
        self._backdrop()
        ui.draw_text(self.screen, "GAMES ON THIS NETWORK", SCREEN_W // 2, 22, 26,
                     UI_ACCENT, anchor="center")
        panel = pygame.Rect(60, 56, SCREEN_W - 120, 252)
        ui.draw_panel(self.screen, panel)
        if self._scanning:
            dots = "." * (1 + int(time.time() * 3) % 3)
            ui.draw_text(self.screen, f"Searching{dots}", panel.centerx,
                         panel.centery, 20, UI_DIM, anchor="center")
        elif not self._servers:
            ui.draw_text(self.screen, "No games found.", panel.centerx,
                         panel.centery - 10, 20, UI_DIM, anchor="center")
            ui.draw_text(self.screen, "Check the host has started and you are on the same network.",
                         panel.centerx, panel.centery + 14, 14, UI_DIM,
                         anchor="center")
        else:
            y = panel.y + 10
            for info in self._servers[:7]:
                host = info.get("host", "?")
                port = int(info.get("port", SO_PORT))
                joinable = info.get("phase") == "lobby"
                ui.draw_text(self.screen, str(info.get("name", "Game"))[:26],
                             panel.x + 12, y + 4, 18, UI_TEXT)
                ui.draw_text(self.screen,
                             f"{host}:{port}  ·  {info.get('map', '?')}",
                             panel.x + 12, y + 20, 13, UI_DIM)
                ui.draw_text(self.screen, f"{info.get('players', 0)}/{info.get('max', 4)}",
                             panel.right - 150, y + 8, 17, UI_TEXT)
                self._button((panel.right - 110, y + 4, 96, 24),
                             "Join" if joinable else "In progress",
                             f"join:{host}:{port}", 15, enabled=joinable)
                y += 34
        self._button((SCREEN_W // 2 - 150, 320, 140, 28), "Back", "back")
        self._button((SCREEN_W // 2 + 10, 320, 140, 28), "Search Again", "rescan")

    def _draw_direct(self) -> None:
        self._backdrop()
        ui.draw_text(self.screen, "CONNECT BY ADDRESS", SCREEN_W // 2, 90, 28,
                     UI_ACCENT, anchor="center")
        self._addr.rect = pygame.Rect(SCREEN_W // 2 - 130, 150, 260, 24)
        self._addr.draw(self.screen, "HOST  (e.g. 192.168.1.40 or pi400.local)")
        ui.draw_text(self.screen, f"Port defaults to {SO_PORT}; use host:port to change it.",
                     SCREEN_W // 2, 186, 14, UI_DIM, anchor="center")
        self._button((SCREEN_W // 2 - 150, 220, 140, 28), "Back", "back")
        self._button((SCREEN_W // 2 + 10, 220, 140, 28), "Connect", "connect")

    def _draw_lobby(self) -> None:
        self._backdrop()
        ui.draw_text(self.screen, self.server_name or "LOBBY", SCREEN_W // 2, 10,
                     24, UI_ACCENT, anchor="center")
        if self.server is not None:
            addrs = ", ".join(discovery.local_addresses()[:2])
            ui.draw_text(self.screen,
                         f"Others join at {addrs} (port {self.server.bound_port})",
                         SCREEN_W // 2, 32, 14, UI_GOOD, anchor="center")

        roster = pygame.Rect(16, 52, 300, 232)
        ui.draw_panel(self.screen, roster)
        ui.draw_text(self.screen, "COMMANDERS", roster.x + 8, roster.y + 6, 15,
                     UI_DIM)
        y = roster.y + 24
        for player in sorted(self.players.values(), key=lambda p: p["pid"]):
            colour = team_color(player.get("color", 0))
            self.screen.fill(colour, (roster.x + 10, y + 4, 8, 8))
            label = player["name"] + (" (you)" if player["pid"] == self.my_pid else "")
            ui.draw_text(self.screen, label, roster.x + 24, y, 17, UI_TEXT)
            if self.settings.teams and len(self.players) == 4:
                ui.draw_text(self.screen, f"T{player.get('team', 0) + 1}",
                             roster.x + 170, y, 15, UI_DIM)
            tag = "BOT" if player["bot"] else ("READY" if player["ready"] else "...")
            ui.draw_text(self.screen, tag, roster.right - 12, y, 14,
                         UI_GOOD if player["ready"] and not player["bot"] else UI_DIM,
                         anchor="topright")
            if self.is_host and player["bot"]:
                self._button((roster.right - 64, y - 1, 16, 16), "x",
                             f"kick:{player['pid']}", 14)
            y += 24

        rules = pygame.Rect(326, 52, SCREEN_W - 342, 232)
        ui.draw_panel(self.screen, rules)
        ui.draw_text(self.screen, "RULES", rules.x + 8, rules.y + 6, 15, UI_DIM)
        rows = [("Map", self.settings.map_name.title(), "map"),
                ("Teams", "2v2" if self.settings.teams else "FFA", "teams"),
                ("Supply", str(self.settings.start_supply), "supply"),
                ("Clock", f"{self.settings.order_time}s"
                 if self.settings.order_time else "off", "clock")]
        ry = rules.y + 26
        for label, value, field in rows:
            ui.draw_text(self.screen, label, rules.x + 10, ry + 2, 16, UI_TEXT)
            if self.is_host:
                self._button((rules.right - 116, ry, 18, 18), "<", f"{field}:-", 15)
                self._button((rules.right - 30, ry, 18, 18), ">", f"{field}:+", 15)
            ui.draw_text(self.screen, value, rules.right - 72, ry + 2, 16,
                         UI_ACCENT, anchor="midtop")
            ry += 26
        notes = self.settings.map_name
        ui.draw_text(self.screen, f"Map: {notes}", rules.x + 10, ry + 8, 13, UI_DIM)

        me = self.players.get(self.my_pid, {})
        self._button((16, 292, 104, 26), "Leave", "leave", 16)
        self._button((126, 292, 104, 26), "Colour", "color", 16)
        self._button((236, 292, 118, 26),
                     "Not Ready" if me.get("ready") else "Ready", "ready", 16)
        if self.is_host:
            self._button((360, 292, 104, 26), "Add Bot", "addbot", 16)
            self._button((470, 292, 154, 26), "START", "start", 17,
                         len(self.players) >= 2)
        self._draw_chat(SCREEN_H - 14, limit=2)

    # -- the match ---------------------------------------------------------
    def _draw_game(self) -> None:
        if self.renderer.board is None:
            self.screen.fill(UI_BG)
            ui.draw_text(self.screen, "Waiting for the match to start...",
                         SCREEN_W // 2, SCREEN_H // 2, 20, UI_DIM, anchor="center")
            return
        self.renderer.draw_terrain(self.screen)
        self.renderer.draw_nodes(self.screen, self.view.node_owner, self.colors)
        self._draw_entities()
        if self.replay is not None:
            visible = self.view.visible | self.replay.extra_visible()
            self.renderer.set_fog(visible, self.view.explored | visible)
        self.renderer.draw_fog(self.screen)
        self._draw_entities_over_fog()
        if self.replay is not None:
            self.replay.draw_effects(self.screen)
        if self.phase == "orders" and self.replay is None:
            self._draw_orders_overlay()
        self._draw_top()
        self._collect_board_tip()
        self._draw_panel()
        self._draw_hud()
        self._draw_chat(SCREEN_H - HUD_H - 6, limit=3)

    def _draw_entities(self) -> None:
        """Buildings and units that sit under the shroud."""
        for building in self.view.buildings.values():
            self.renderer.draw_building(
                self.screen, building, team_color(self.colors.get(building["owner"], 0)))
        for ghost in self.view.remembered.values():
            self.renderer.draw_unit(
                self.screen, ghost,
                team_color(self.colors.get(ghost["owner"], 0)),
                facing_left=self.view.face_left(ghost["uid"]), ghost=True)

    def _draw_entities_over_fog(self) -> None:
        """Live units draw above the shroud so a spotted enemy is never dimmed."""
        for unit in self.view.units.values():
            colour = team_color(self.colors.get(unit["owner"], 0))
            pos = self.replay.unit_pixel(unit) if self.replay else None
            self.renderer.draw_unit(self.screen, unit, colour,
                                    selected=unit["uid"] in self.selected,
                                    pos=pos,
                                    facing_left=self.view.face_left(unit["uid"]))

    def _draw_orders_overlay(self) -> None:
        board = self.renderer.board
        colour = team_color(self.colors.get(self.my_pid, 0))
        # Orders stand until changed, so a unit still walking out last turn's
        # march shows its remaining route -- dimmed, to separate "already
        # marching" from "about to be told to".
        for unit in self.view.mine(self.my_pid):
            if unit["uid"] in self.unit_orders:
                continue
            remaining = [tuple(t) for t in unit.get("path", [])]
            if remaining:
                self.renderer.draw_order_path(
                    self.screen, (unit["x"], unit["y"]), remaining,
                    shade(colour, 0.55), unit.get("stance") == "attack")
        for uid, order in self.unit_orders.items():
            unit = self.view.units.get(uid)
            if unit is None:
                continue
            colour = team_color(self.colors.get(self.my_pid, 0))
            self.renderer.draw_order_path(self.screen, (unit["x"], unit["y"]),
                                          order.get("path", []), colour,
                                          order["o"] == "attack")
        for order in self.queued:
            if order["o"] == "build":
                self.renderer.highlight(self.screen, tuple(order["to"]), UI_ACCENT)
        if self.placing:
            tile = board.to_tile(self.mouse)
            if tile is not None:
                ok = board.map.passable(*tile)
                self.renderer.highlight(self.screen, tile,
                                        UI_GOOD if ok else UI_WARN)
        if self.drag_anchor is not None:
            self.renderer.draw_selection_box(self.screen, self.drag_anchor,
                                             self.mouse)

    def _collect_board_tip(self) -> None:
        """Describe whatever the pointer is over, so nobody has to decode the
        art by experiment."""
        board = self.renderer.board
        tile = board.to_tile(self.mouse)
        if tile is None:
            return
        known = tile in self.view.explored
        lines: list = []

        unit = self.view.unit_at(tile) or self.view.remembered.get(
            next((uid for uid, g in self.view.remembered.items()
                  if (g["x"], g["y"]) == tile), None))
        building = self.view.building_at(tile)

        if unit is not None:
            info = UNIT[unit["code"]]
            owner = self.players.get(unit["owner"], {}).get("name", "?")
            mine = unit["owner"] == self.my_pid
            lines.append((f"{info.name}  ({owner})", team_color(
                self.colors.get(unit["owner"], 0)), 17))
            lines.append((f"{unit.get('hp', info.hp)}/{info.hp} hp   "
                          f"speed {info.speed}   range {info.reach}",
                          UI_TEXT, 14))
            if info.beats:
                lines.append((f"Strong against {UNIT[info.beats].name}",
                              UI_GOOD, 14))
            loser = next((u.name for u in UNIT.values() if u.beats == info.code),
                         None)
            if loser:
                lines.append((f"Weak against {loser}", UI_WARN, 14))
            if unit.get("ghost"):
                lines.append(("Last known position", UI_DIM, 13))
            elif mine:
                lines.append((info.blurb, UI_DIM, 13))
        elif building is not None:
            info = BUILDING[building["code"]]
            owner = self.players.get(building["owner"], {}).get("name", "?")
            lines.append((f"{info.name}  ({owner})", team_color(
                self.colors.get(building["owner"], 0)), 17))
            if building.get("under"):
                lines.append((f"Under construction: {building['under']} turns",
                              UI_ACCENT, 14))
            else:
                lines.append((f"{building['hp']}/{info.hp} hp", UI_TEXT, 14))
                if info.produces:
                    made = ", ".join(UNIT[c].name for c in info.produces)
                    lines.append((f"Trains {made}", UI_TEXT, 14))
                if info.income:
                    lines.append((f"+{info.income} supply each turn", UI_GOOD, 14))
            if building["code"] == "base":
                lines.append(("Lose it and you are out", UI_DIM, 13))
        else:
            char = board.map.at(*tile)
            name, note = describe(char)
            lines.append((name, UI_TEXT, 17))
            if note:
                lines.append((note, UI_DIM, 14))
            if board.map.is_node(tile[0], tile[1]):
                holder = self.view.node_owner.get(tile)
                if holder is None:
                    lines.append(("Unclaimed", UI_ACCENT, 14))
                else:
                    who = self.players.get(holder, {}).get("name", "?")
                    lines.append((f"Held by {who}", team_color(
                        self.colors.get(holder, 0)), 14))
                lines.append((f"+{HARVEST_RATE} a turn with an Engineer on it",
                              UI_GOOD, 14))
                lines.append((f"...and a depot within {HARVEST_RADIUS} tiles",
                              UI_DIM, 13))
            if not known:
                lines.append(("Unscouted", UI_DIM, 13))

        self._tips.append((board.rect(tile), lines))

    def _draw_top(self) -> None:
        strip = pygame.Surface((SCREEN_W, TOP_H), pygame.SRCALPHA)
        strip.fill((10, 10, 16, 190))
        self.screen.blit(strip, (0, 0))
        ui.draw_text(self.screen, f"TURN {self.turn}", 6, 2, 15, UI_TEXT)
        phase = {"orders": "WRITING ORDERS", "resolve": "EXECUTING",
                 "over": "FINISHED"}.get(self.phase, self.phase.upper())
        ui.draw_text(self.screen, phase, SCREEN_W // 2, 2, 15,
                     UI_ACCENT if self.phase == "orders" else UI_GOOD,
                     anchor="midtop")
        if self.phase == "orders" and self.time_left >= 0:
            urgent = self.time_left <= 10
            ui.draw_text(self.screen, f"{int(self.time_left):>3}s", SCREEN_W - 6,
                         2, 15, UI_WARN if urgent else UI_DIM, anchor="topright")
        elif self.replay is not None:
            width = int(60 * self.replay.progress())
            self.screen.fill(UI_PANEL_LO, (SCREEN_W - 68, 6, 60, 4))
            self.screen.fill(UI_ACCENT, (SCREEN_W - 68, 6, width, 4))

    def _draw_panel(self) -> None:
        panel = pygame.Rect(SCREEN_W - PANEL_W, TOP_H, PANEL_W,
                            SCREEN_H - TOP_H - HUD_H)
        ui.draw_panel(self.screen, panel, UI_PANEL)
        x = panel.x + 8
        y = panel.y + 6

        if self.replay is not None:
            ui.draw_text(self.screen, "EXECUTING", x, y, 17, UI_ACCENT)
            y += 20
            for line in self.log[-6:]:
                ui.draw_text(self.screen, line[:26], x, y, 13, UI_TEXT)
                y += 14
            self._button((panel.x + 8, panel.bottom - 30, PANEL_W - 16, 24),
                         "Skip (Space)", "skip", 15)
            return

        if self.selected_building is not None:
            self._draw_production(panel, x, y)
        elif self._selected_workers():
            self._draw_engineering(panel, x, y)
        elif self.selected:
            self._draw_selection(panel, x, y)
        else:
            self._draw_overview(panel, x, y)

    def _selected_workers(self) -> list:
        return [self.view.units[uid] for uid in sorted(self.selected)
                if uid in self.view.units
                and UNIT[self.view.units[uid]["code"]].builder]

    def _draw_engineering(self, panel, x, y) -> None:
        """What a selected Engineer can raise."""
        workers = self._selected_workers()
        ui.draw_text(self.screen, f"{len(workers)} ENGINEER"
                     f"{'S' if len(workers) != 1 else ''}", x, y, 17, UI_ACCENT)
        y += 18
        job = next((w.get("job") for w in workers if w.get("job")), None)
        if job:
            ui.draw_text(self.screen, f"building a {BUILDING[job[2]].name}",
                         x, y, 13, UI_GOOD)
        else:
            ui.draw_text(self.screen, "click a structure, then a tile", x, y,
                         13, UI_DIM)
        y += 16
        done = self._my_research()
        for info in BUILDINGS:
            if not info.buildable:
                continue
            price = cost_of_building(info.code, done)
            affordable = self._spent() + price <= self.view.supply
            rect = pygame.Rect(x, y, PANEL_W - 16, 24)
            chosen = self.placing == info.code
            ui.draw_panel(self.screen, rect,
                          UI_PANEL_HI if (affordable or chosen)
                          else shade(UI_PANEL, 0.8))
            if chosen:
                pygame.draw.rect(self.screen, UI_ACCENT, rect, 1)
            ui.draw_text(self.screen, info.name, rect.x + 5, rect.y + 4, 15,
                         UI_TEXT if affordable else UI_DIM)
            ui.draw_text(self.screen, f"{price}s {info.build_turns}t",
                         rect.right - 5, rect.y + 4, 13, UI_DIM,
                         anchor="topright")
            self._button(rect, "", f"place:{info.code}", hidden=True,
                         enabled=affordable)
            self._tips.append((rect, self._structure_tip(info.code)))
            y += 27
        ui.draw_text(self.screen, "Engineers on a node inside", x, y + 4, 12,
                     UI_DIM)
        ui.draw_text(self.screen, f"{HARVEST_RADIUS} tiles of a depot send supply.",
                     x, y + 16, 12, UI_DIM)

    def _structure_tip(self, code: str) -> list:
        info = BUILDING[code]
        done = self._my_research()
        lines = [(info.name, UI_ACCENT, 17),
                 (f"{cost_of_building(code, done)} supply   "
                  f"{info.build_turns} turns", UI_TEXT, 14),
                 (f"{info.hp} hp", UI_TEXT, 14)]
        if info.supply_cap:
            lines.append((f"+{info.supply_cap} army cap", UI_GOOD, 14))
        if info.harvests:
            lines.append((f"Receives supply within {HARVEST_RADIUS} tiles",
                          UI_GOOD, 14))
        if info.attack:
            lines.append((f"Attack {info.attack}, range {info.reach}",
                          UI_TEXT, 14))
        if info.produces:
            lines.append(("Trains " + ", ".join(UNIT[c].name
                                                for c in info.produces),
                          UI_TEXT, 14))
        if info.wall:
            lines.append(("Bruisers and Engineers break it fast", UI_WARN, 14))
        lines.append((info.blurb, UI_DIM, 13))
        return lines

    def _draw_research(self, panel, x, y, building) -> int:
        """Research rows under the Command Post's production list."""
        done = self._my_research()
        pending = next((o for o in self.queued if o["o"] == "research"), None)
        active = building.get("project") or (
            [pending["code"], RESEARCH_BY_CODE[pending["code"]].turns]
            if pending else None)
        ui.draw_text(self.screen, "RESEARCH", x, y, 14, UI_DIM)
        y += 15
        if active:
            project = RESEARCH_BY_CODE.get(active[0])
            label = project.name if project else active[0]
            ui.draw_text(self.screen, f"{label}: {active[1]} turns", x, y, 14,
                         UI_ACCENT)
            return y + 16
        options = available_research(done)
        if not options:
            ui.draw_text(self.screen, "all complete", x, y, 13, UI_GOOD)
            return y + 16
        for project in options[:4]:
            affordable = self._spent() + project.cost <= self.view.supply
            rect = pygame.Rect(x, y, PANEL_W - 16, 20)
            ui.draw_panel(self.screen, rect,
                          UI_PANEL_HI if affordable else shade(UI_PANEL, 0.8))
            ui.draw_text(self.screen, project.name, rect.x + 5, rect.y + 2, 14,
                         UI_TEXT if affordable else UI_DIM)
            ui.draw_text(self.screen, f"{project.cost}s", rect.right - 5,
                         rect.y + 2, 13, UI_DIM, anchor="topright")
            self._button(rect, "", f"research:{project.code}", hidden=True,
                         enabled=affordable)
            self._tips.append((rect, [
                (project.name, UI_ACCENT, 17),
                (f"{project.cost} supply   {project.turns} turns", UI_TEXT, 14),
                (project.blurb, UI_GOOD, 14),
            ]))
            y += 23
        return y

    def _draw_production(self, panel, x, y) -> None:
        building = self.view.buildings.get(self.selected_building)
        if building is None:
            self.selected_building = None
            return
        btype = BUILDING[building["code"]]
        ui.draw_text(self.screen, btype.name.upper(), x, y, 17, UI_ACCENT)
        y += 18
        if building.get("under"):
            ui.draw_text(self.screen, f"under construction: {building['under']} turns",
                         x, y, 13, UI_DIM)
            return
        queue = building.get("queue", [])
        ui.draw_text(self.screen, f"queue: {len(queue)}", x, y, 13, UI_DIM)
        y += 16
        for code in btype.produces:
            unit = UNIT[code]
            affordable = self._spent() + unit.cost <= self.view.supply
            rect = pygame.Rect(x, y, PANEL_W - 16, 26)
            ui.draw_panel(self.screen, rect,
                          UI_PANEL_HI if affordable else shade(UI_PANEL, 0.8))
            ui.draw_text(self.screen, unit.name, rect.x + 5, rect.y + 2, 15,
                         UI_TEXT if affordable else UI_DIM)
            ui.draw_text(self.screen, f"{unit.cost}s  {unit.build_turns}t",
                         rect.right - 5, rect.y + 2, 13, UI_DIM, anchor="topright")
            ui.draw_text(self.screen, unit.blurb[:30], rect.x + 5, rect.y + 14, 11,
                         UI_DIM)
            self._button(rect, "", f"train:{code}", hidden=True, enabled=affordable)
            self._tips.append((rect, self._unit_tip(code)))
            y += 29
        if building["code"] == "base":
            y = self._draw_research(panel, x, y + 4, building) + 4
            barracks = BUILDING["barracks"]
            rect = pygame.Rect(x, y, PANEL_W - 16, 24)
            ui.draw_panel(self.screen, rect, UI_PANEL_HI)
            ui.draw_text(self.screen, f"Build {barracks.name}", rect.x + 5,
                         rect.y + 4, 15, UI_TEXT)
            ui.draw_text(self.screen, f"{barracks.cost}s", rect.right - 5,
                         rect.y + 4, 13, UI_DIM, anchor="topright")
            self._button(rect, "", "place:barracks", hidden=True)
            self._tips.append((rect, self._structure_tip("barracks")))

    def _unit_tip(self, code: str) -> list:
        info = UNIT[code]
        lines = [(info.name, UI_ACCENT, 17),
                 (f"{info.cost} supply   {info.build_turns} turn"
                  f"{'s' if info.build_turns != 1 else ''} to build", UI_TEXT, 14),
                 (f"{info.hp} hp   attack {info.attack}   speed {info.speed}"
                  f"   range {info.reach}", UI_TEXT, 14)]
        if info.beats:
            lines.append((f"Strong against {UNIT[info.beats].name}", UI_GOOD, 14))
        loser = next((u.name for u in UNIT.values() if u.beats == code), None)
        if loser:
            lines.append((f"Weak against {loser}", UI_WARN, 14))
        lines.append((info.blurb, UI_DIM, 13))
        return lines

    def _draw_selection(self, panel, x, y) -> None:
        units = [self.view.units[uid] for uid in sorted(self.selected)
                 if uid in self.view.units]
        ui.draw_text(self.screen, f"{len(units)} SELECTED", x, y, 17, UI_ACCENT)
        y += 20
        tally: dict[str, int] = {}
        for unit in units:
            tally[unit["code"]] = tally.get(unit["code"], 0) + 1
        for code, count in sorted(tally.items()):
            unit_type = UNIT[code]
            ui.draw_text(self.screen, f"{count}x {unit_type.name}", x, y, 16, UI_TEXT)
            beats = f"beats {UNIT[unit_type.beats].name}" if unit_type.beats else "no counter"
            ui.draw_text(self.screen, f"spd {unit_type.speed}  rng {unit_type.reach}  {beats}",
                         x, y + 14, 12, UI_DIM)
            y += 30
        y = max(y, panel.y + 120)
        ui.draw_text(self.screen, "right-click: move", x, y, 13, UI_DIM)
        ui.draw_text(self.screen, "shift+right-click: attack-move", x, y + 14, 13,
                     UI_DIM)
        ui.draw_text(self.screen, "tab: select whole army", x, y + 28, 13, UI_DIM)

    def _draw_overview(self, panel, x, y) -> None:
        ui.draw_text(self.screen, "COMMANDERS", x, y, 15, UI_DIM)
        y += 18
        for row in sorted(self.standings or [], key=lambda r: r["pid"]):
            colour = team_color(row.get("color", 0))
            self.screen.fill(colour if row.get("alive") else shade(colour, 0.35),
                             (x, y + 3, 7, 7))
            name = row["name"][:11] + ("" if row.get("alive") else " (out)")
            ui.draw_text(self.screen, name, x + 12, y, 14,
                         UI_TEXT if row.get("alive") else UI_DIM)
            ui.draw_text(self.screen, f"{row.get('units', 0)}u", panel.right - 10,
                         y, 13, UI_DIM, anchor="topright")
            y += 17
        y += 6
        ui.draw_text(self.screen, "LOG", x, y, 15, UI_DIM)
        y += 16
        for line in self.log[-5:]:
            ui.draw_text(self.screen, line[:26], x, y, 12, UI_TEXT)
            y += 13
        if self.rejected:
            y += 4
            ui.draw_text(self.screen, "REFUSED", x, y, 14, UI_WARN)
            y += 15
            for line in self.rejected[:3]:
                ui.draw_text(self.screen, line[:28], x, y, 12, UI_WARN)
                y += 13

    def _draw_hud(self) -> None:
        bar = pygame.Rect(0, SCREEN_H - HUD_H, SCREEN_W, HUD_H)
        ui.draw_panel(self.screen, bar, UI_PANEL)
        colour = team_color(self.colors.get(self.my_pid, 0))
        me = self.players.get(self.my_pid, {})
        self.screen.fill(colour, (8, bar.y + 8, 10, 10))
        ui.draw_text(self.screen, me.get("name", "?"), 24, bar.y + 5, 18, colour)

        available = self.view.supply - self._spent()
        ui.draw_text(self.screen, "SUPPLY", 130, bar.y + 4, 12, UI_DIM)
        ui.draw_text(self.screen, str(available), 130, bar.y + 15, 20,
                     UI_GOOD if available >= 0 else UI_WARN)
        if self._spent():
            ui.draw_text(self.screen, f"-{self._spent()} queued", 170, bar.y + 20,
                         12, UI_DIM)

        ui.draw_text(self.screen, "ARMY", 236, bar.y + 4, 12, UI_DIM)
        cap = self._army_cap()
        size = self._army_size()
        ui.draw_text(self.screen, f"{size}/{cap}", 236, bar.y + 15, 20,
                     UI_WARN if cap and size >= cap else UI_TEXT)

        ui.draw_text(self.screen, "NODES", 310, bar.y + 4, 12, UI_DIM)
        held = sum(1 for owner in self.view.node_owner.values()
                   if owner == self.my_pid)
        ui.draw_text(self.screen, str(held), 310, bar.y + 15, 20, UI_TEXT)

        orders = len(self.unit_orders) + len(self.queued)
        ui.draw_text(self.screen, "ORDERS", 366, bar.y + 4, 12, UI_DIM)
        ui.draw_text(self.screen, str(orders), 366, bar.y + 15, 20,
                     UI_ACCENT if orders else UI_DIM)

        if self.phase == "orders" and self.replay is None:
            self._button((SCREEN_W - 130, bar.y + 8, 120, 28),
                         "WAITING..." if self.ready_sent else "COMMIT (Enter)",
                         "unready" if self.ready_sent else "ready", 16)
            self._button((SCREEN_W - 210, bar.y + 8, 74, 28), "Clear", "clear", 15)

    def _draw_chat(self, bottom: int, limit: int = 3) -> None:
        y = bottom
        for name, text, colour in reversed(self.chat[-limit:]):
            ui.draw_text(self.screen, f"{name}: {text}", 6, y, 14, colour,
                         shadow=True)
            y -= 14

    def _draw_pause(self) -> None:
        self._draw_game()
        veil = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
        veil.fill((8, 8, 14, 195))
        self.screen.blit(veil, (0, 0))
        ui.draw_text(self.screen, "PAUSED", SCREEN_W // 2, 100, 38, UI_ACCENT,
                     anchor="center")
        ui.draw_text(self.screen, "The turn clock keeps running without you.",
                     SCREEN_W // 2, 136, 14, UI_DIM, anchor="center")
        self._button((SCREEN_W // 2 - 150, 176, 140, 30), "Resume", "resume")
        self._button((SCREEN_W // 2 + 10, 176, 140, 30), "Leave Game", "leave")
        card = pygame.Rect(SCREEN_W // 2 - 170, 224, 340, 132)
        ui.draw_panel(self.screen, card)
        lines = [
            "left-click        select a unit or building",
            "drag              box-select an army",
            "right-click       move there",
            "shift+right       attack-move there",
            "Tab               select your whole army",
            "Enter             commit your orders",
            "Space             skip a replay",
            "T                 chat",
        ]
        y = card.y + 8
        for line in lines:
            ui.draw_text(self.screen, line, card.x + 12, y, 14, UI_TEXT)
            y += 15

    def _draw_over(self) -> None:
        self._backdrop()
        won = (self.winner_team is not None
               and self.players.get(self.my_pid, {}).get("team") == self.winner_team)
        ui.draw_text(self.screen, "VICTORY" if won else "DEFEAT", SCREEN_W // 2,
                     28, 40, UI_GOOD if won else UI_WARN, anchor="center")
        panel = pygame.Rect(140, 78, SCREEN_W - 280, 216)
        ui.draw_panel(self.screen, panel)
        y = panel.y + 12
        for row in sorted(self.standings,
                          key=lambda r: (not r.get("alive"), r["pid"])):
            colour = team_color(row.get("color", 0))
            self.screen.fill(colour, (panel.x + 12, y + 4, 8, 8))
            ui.draw_text(self.screen, row["name"], panel.x + 28, y, 18,
                         UI_TEXT if row.get("alive") else UI_DIM)
            ui.draw_text(self.screen, "survived" if row.get("alive") else "knocked out",
                         panel.right - 12, y, 15, UI_DIM, anchor="topright")
            y += 26
        if self.is_host:
            self._button((SCREEN_W // 2 - 150, 306, 140, 28), "Play Again", "again")
        self._button((SCREEN_W // 2 + 10, 306, 140, 28), "Leave", "leave")


def _default_name() -> str:
    for key in ("SCORCHED_NAME", "USER", "USERNAME", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            return value[:14]
    return "Commander"


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="standing_orders",
                                     description="Standing Orders - LAN skirmish RTS")
    parser.add_argument("--name", default="")
    parser.add_argument("--connect", default="", metavar="HOST[:PORT]")
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--bots", type=int, default=1)
    parser.add_argument("--skill", default="moderate", choices=SKILLS)
    parser.add_argument("--map", default="duel")
    parser.add_argument("--teams", action="store_true")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--no-sound", action="store_true")
    parser.add_argument("--no-scale", action="store_true")
    args = parser.parse_args(argv)

    app = App(name=args.name, fullscreen=args.fullscreen,
              sound=not args.no_sound, scaled=not args.no_scale)
    app._bots = max(0, args.bots)
    app._skill = args.skill
    app.settings = Settings(map_name=args.map, teams=args.teams)
    if args.connect:
        host, _, port = args.connect.partition(":")
        app.join_game(host, int(port) if port.isdigit() else SO_PORT)
    elif args.host:
        app.host_game(app.settings, args.bots, args.skill)
    app.run()
    return 0
