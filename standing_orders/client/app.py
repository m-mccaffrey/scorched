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
from ..units import (AIRSTRIKE_COST, AIRSTRIKE_RADIUS, BUILDING, BUILDINGS,
                     HARVEST_RADIUS, HARVEST_RATE, MEDIC_HEAL_COST,
                     RESEARCH_BY_CODE, UNIT, available_research,
                     cost_of_building, catalogue, promotion_cost, rank_name)
from .audio import OrdersSfx
from .render import (HUD_H, PANEL_W, SCREEN_H, SCREEN_W, TOP_H, Board,
                     Renderer,
                     draw_tooltip)
from .replay import ReplayPlayer
from .view import WorldView

FPS = 60
SKILLS = ("novice", "moderate", "veteran", "cyborg")

#: Camera scroll speed, in board pixels a second. A whole screen takes a little
#: under a second to cross, which is quick enough not to nag and slow enough
#: that you can follow where you have got to.
SCROLL_SPEED = 520

#: How close to the edge of the board the pointer has to be to pan.
EDGE_PAN = 12

#: Supply sent by one press of the gift button. Small enough to be a gesture
#: and large enough to matter when somebody is on the ropes.
GIFT_SIZE = 15

#: Height reserved at the foot of the command panel for the minimap. The
#: tightest panel state (a Command Post, with production and research) draws to
#: within 75px of the bottom, so this has to fit inside that and leave a margin.
MINIMAP_BAND = 68


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
        self.aiming: int | None = None    # Airfield bid awaiting an aim point
        self.parley = False               # the diplomacy table is open
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
            for line in self.replay.notes:
                if line not in self.log:
                    self.log.append(line)
            del self.log[:-8]
            if not self.replay.update(dt):
                self._finish_replay()
        if self.time_left > 0:
            self.time_left = max(0.0, self.time_left - dt)
        if self.mode == "game":
            self._scroll_camera(dt)

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
        self.aiming = None
        self.parley = False
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
            if self.parley:
                self.parley = False
            elif self.aiming is not None:
                self.aiming = None
                self.status = "Airstrike called off"
            elif self.placing:
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
        elif key == pygame.K_p and self.selected:
            self._queue_promotions()
        elif key == pygame.K_TAB:
            self._select_all_units()
        elif key == pygame.K_HOME:
            self._look_at_home()
        elif key == pygame.K_g:
            self.parley = not self.parley

    # -- camera ------------------------------------------------------------
    def _scroll_camera(self, dt: float) -> None:
        """Arrow keys, WASD, and the screen edge.

        Held keys rather than key events: a camera that steps once per repeat
        is the same jitter the artillery aim had, and it reads as broken.
        """
        board = self.renderer.board
        if board is None or not board.scrolls:
            return
        keys = pygame.key.get_pressed()
        dx = dy = 0
        if keys[pygame.K_LEFT] or keys[pygame.K_a]:
            dx -= 1
        if keys[pygame.K_RIGHT] or keys[pygame.K_d]:
            dx += 1
        if keys[pygame.K_UP] or keys[pygame.K_w]:
            dy -= 1
        if keys[pygame.K_DOWN] or keys[pygame.K_s]:
            dy += 1
        # Nudging the edge of the board with the pointer scrolls too, which is
        # what a hand already on the mouse expects.
        if Board.VIEW.collidepoint(self.mouse):
            if self.mouse[0] - Board.VIEW.x < EDGE_PAN:
                dx -= 1
            elif Board.VIEW.right - self.mouse[0] < EDGE_PAN:
                dx += 1
            if self.mouse[1] - Board.VIEW.y < EDGE_PAN:
                dy -= 1
            elif Board.VIEW.bottom - self.mouse[1] < EDGE_PAN:
                dy += 1
        if dx or dy:
            step = int(SCROLL_SPEED * dt) or 1
            board.scroll_by(dx * step, dy * step)

    def _look_at_home(self) -> None:
        """Snap the camera to your Command Post."""
        board = self.renderer.board
        base = next((b for b in self.view.buildings.values()
                     if b["owner"] == self.my_pid and b["code"] == "base"), None)
        if board is not None and base is not None:
            board.centre_on((base["x"], base["y"]))

    def _minimap_rect(self) -> pygame.Rect | None:
        """Where the minimap sits: a reserved band at the foot of the panel.

        Reserved in every panel state, not just the overview. You almost always
        have something selected while giving orders, which is precisely when
        knowing where the fighting is matters, so a minimap that only appears
        when nothing is selected would be missing whenever it was wanted.
        """
        board = self.renderer.board
        if board is None:
            return None
        panel = pygame.Rect(SCREEN_W - PANEL_W, TOP_H, PANEL_W,
                            SCREEN_H - TOP_H - HUD_H)
        band = pygame.Rect(panel.x + 6, panel.bottom - MINIMAP_BAND,
                           panel.width - 12, MINIMAP_BAND - 6)
        # Fit the band, preserving the map's shape. Snapping to whole pixels a
        # tile would waste most of the band on a wide map -- a 64x44 map would
        # take one pixel a tile and fill 64 of the 176 pixels available -- and
        # this is a picture to glance at, not a grid to read.
        scale = min(band.width / board.map.width, band.height / board.map.height)
        width = max(1, int(board.map.width * scale))
        height = max(1, int(board.map.height * scale))
        return pygame.Rect(band.x + (band.width - width) // 2,
                           band.bottom - height, width, height)

    def _minimap_click(self, pos) -> bool:
        """Click or drag the minimap to move the camera. True if it was ours."""
        rect = self._minimap_rect()
        board = self.renderer.board
        if rect is None or board is None or not rect.collidepoint(pos):
            return False
        tx = int((pos[0] - rect.x) / rect.width * board.map.width)
        ty = int((pos[1] - rect.y) / rect.height * board.map.height)
        board.centre_on((tx, ty))
        return True

    def _select_all_units(self) -> None:
        self.selected = {u["uid"] for u in self.view.mine(self.my_pid)}
        self.selected_building = None

    def _game_click(self, event) -> None:
        board = self.renderer.board
        if board is None:
            return
        if self.parley:
            if event.button == 1:
                for button in self._buttons:
                    if button.clicked(self.mouse):
                        self.sfx.play("click")
                        self._panel_action(button.action)
                        return
            return
        tile = board.to_tile(event.pos)

        if event.button == 1:
            if self._minimap_click(event.pos):
                return
            if tile is not None and self.aiming is not None:
                self._call_airstrike(tile)
                return
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
        # Only structures are planned around -- matching the server, which
        # ignores units because they will all have moved by the time the plan
        # runs. Formations still avoid stacking on occupied ground.
        structures = {(b["x"], b["y"]) for b in self.view.buildings.values()}
        occupied = structures | {(u["x"], u["y"]) for u in self.view.units.values()}
        targets = self._formation(tile, len(self.selected), occupied)
        units = sorted(
            (self.view.units[uid] for uid in self.selected if uid in self.view.units),
            key=lambda u: abs(u["x"] - tile[0]) + abs(u["y"] - tile[1]))
        for unit, goal in zip(units, targets):
            path = find_path(board.map, (unit["x"], unit["y"]), goal,
                             structures - {goal})
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
        elif action == "airstrike":
            self._begin_airstrike()
        elif action == "promote":
            self._queue_promotions()
        elif action == "parley":
            self.parley = not self.parley
        elif action.startswith("dip:"):
            self._queue_diplomacy(action.split(":")[1:])
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

    def _begin_airstrike(self) -> None:
        """Arm the selected Airfield and wait for an aim point."""
        if self.selected_building is None:
            return
        building = self.view.buildings.get(self.selected_building)
        if building is None or not BUILDING[building["code"]].airstrikes:
            return
        if any(o["o"] == "airstrike" and o["bid"] == building["bid"]
               for o in self.queued):
            self._complain("That Airfield has already flown today")
            self.sfx.play("deny")
            return
        if self._spent() + AIRSTRIKE_COST > self.view.supply:
            self._complain("Not enough supply")
            self.sfx.play("deny")
            return
        self.aiming = building["bid"]
        self.status = "Airstrike: click the target tile (Esc to call it off)"

    def _call_airstrike(self, tile) -> None:
        bid, self.aiming = self.aiming, None
        if bid is None:
            return
        self.queued.append({"o": "airstrike", "bid": bid, "to": list(tile)})
        self.status = (f"Airstrike called on {tile[0]},{tile[1]} -- it lands "
                       "halfway through the turn")
        self.sfx.play("order")
        self.ready_sent = False

    # -- diplomacy ----------------------------------------------------------
    def _queue_diplomacy(self, parts: list) -> None:
        """Queue one diplomatic order from the table.

        These replace rather than stack: offering a truce and then an alliance
        to the same commander in one turn should send the second, not both.
        """
        kind = parts[0]
        target = int(parts[1]) if len(parts) > 1 else -1
        self.queued = [o for o in self.queued
                       if not (o["o"] in ("propose", "accept", "declare", "gift")
                               and o.get("to", o.get("from")) == target)]
        if kind == "armistice":
            calling = self.my_pid in self.view.armistice
            pending = next((o for o in self.queued if o["o"] == "armistice"), None)
            if pending is not None:
                self.queued.remove(pending)
                want = not pending.get("on", True)
            else:
                want = not calling
            self.queued.append({"o": "armistice", "on": want})
            self.status = ("Calling for an armistice -- it takes everyone"
                           if want else "Armistice call withdrawn")
        elif kind in ("truce", "alliance"):
            self.queued.append({"o": "propose", "to": target, "pact": kind})
            self.status = f"Offering {'an' if kind[0] == 'a' else 'a'} {kind}"
        elif kind == "accept":
            self.queued.append({"o": "accept", "from": target})
            self.status = "Accepting"
        elif kind == "declare":
            self.queued.append({"o": "declare", "to": target})
            self.status = "War declared -- it takes effect at the end of the turn"
        elif kind == "gift":
            if self._spent() + GIFT_SIZE > self.view.supply:
                self._complain("Not enough supply")
                self.sfx.play("deny")
                return
            self.queued.append({"o": "gift", "to": target, "supply": GIFT_SIZE})
            self.status = f"Sending {GIFT_SIZE} supply"
        self.sfx.play("order")
        self.ready_sent = False

    def _draw_parley(self) -> None:
        """The diplomacy table: who stands where, and what you can offer them.

        Everything on it is public. In a game played round one table a secret
        alliance is just a conversation, and a betrayal nobody could see coming
        is a feel-bad rather than a twist -- so pacts, offers, declarations and
        the armistice call are all visible to everybody.
        """
        self._draw_game()
        veil = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
        veil.fill((6, 7, 12, 238))
        self.screen.blit(veil, (0, 0))
        ui.draw_text(self.screen, "THE TABLE", SCREEN_W // 2, 10, 24, UI_ACCENT,
                     anchor="midtop")
        ui.draw_text(self.screen,
                     "the war ends when every commander calls it",
                     SCREEN_W // 2, 36, 13, UI_DIM, anchor="midtop")
        ui.draw_text(self.screen, "everyone keeps what they hold",
                     SCREEN_W // 2, 48, 13, UI_DIM, anchor="midtop")

        others = [row for row in sorted(self.standings or [],
                                        key=lambda r: r["pid"])
                  if row["pid"] != self.my_pid and row.get("alive")]
        y = 66
        for row in others:
            pid = row["pid"]
            pact = self.view.pact_with(self.my_pid, pid)
            breaking = self.view.breaking_with(self.my_pid, pid)
            offered = self.view.offer_from(pid, self.my_pid)
            mine_offered = self.view.offer_from(self.my_pid, pid)

            card = pygame.Rect(40, y, SCREEN_W - 80, 72)
            ui.draw_panel(self.screen, card, UI_PANEL)
            self.screen.fill(team_color(row.get("color", 0)),
                             (card.x + 10, card.y + 12, 12, 12))
            ui.draw_text(self.screen, row["name"][:14], card.x + 30, card.y + 8,
                         19, UI_TEXT)
            state_text, tint = {
                "war": ("AT WAR", UI_WARN),
                "truce": ("TRUCE", UI_TEXT),
                "alliance": ("ALLIED", UI_GOOD),
            }[pact]
            if breaking:
                state_text, tint = "WAR DECLARED -- effective this turn", UI_WARN
            ui.draw_text(self.screen, state_text, card.x + 30, card.y + 28, 14, tint)
            ui.draw_text(self.screen,
                         f"{row.get('units', 0)} units   "
                         f"{self.view.ground.get(pid, 0)} ground",
                         card.x + 30, card.y + 46, 13, UI_DIM)
            if pid in self.view.armistice:
                ui.draw_text(self.screen, "calling for an armistice",
                             card.right - 10, card.y + 46, 13, UI_GOOD,
                             anchor="topright")
            if mine_offered:
                ui.draw_text(self.screen, f"you offered {mine_offered}",
                             card.right - 10, card.y + 8, 13, UI_ACCENT,
                             anchor="topright")

            buttons = []
            if offered:
                buttons.append((f"Accept {offered}", f"dip:accept:{pid}"))
            if pact == "war":
                buttons.append(("Offer truce", f"dip:truce:{pid}"))
                buttons.append(("Offer alliance", f"dip:alliance:{pid}"))
            elif not breaking:
                buttons.append(("Declare war", f"dip:declare:{pid}"))
                if pact == "truce":
                    buttons.append(("Offer alliance", f"dip:alliance:{pid}"))
            buttons.append((f"Gift {GIFT_SIZE}", f"dip:gift:{pid}"))

            bx = card.right - 8
            for label, action in reversed(buttons):
                width = 96 if len(label) < 14 else 116
                bx -= width + 6
                self._button((bx, card.y + 24, width, 26), label, action, 14)
            y += 78

        calling = self.my_pid in self.view.armistice
        pending = any(o["o"] == "armistice" for o in self.queued)
        want = next((o.get("on", True) for o in self.queued
                     if o["o"] == "armistice"), calling)
        agreed = sum(1 for row in (self.standings or [])
                     if row.get("alive") and row["pid"] in self.view.armistice)
        living = sum(1 for row in (self.standings or []) if row.get("alive"))
        ui.draw_text(self.screen, f"armistice: {agreed} of {living} commanders",
                     48, SCREEN_H - 48, 14, UI_GOOD if agreed else UI_DIM)
        self._button((SCREEN_W - 250, SCREEN_H - 54, 190, 28),
                     "Withdraw the call" if want else "Call for an armistice",
                     "dip:armistice", 15)
        if pending:
            ui.draw_text(self.screen, "queued", SCREEN_W - 250, SCREEN_H - 70,
                         13, UI_ACCENT)
        ui.draw_text(self.screen, "G or Esc to close", 48, SCREEN_H - 28, 13,
                     UI_DIM)

    def _queue_promotions(self) -> None:
        """Promote every selected unit that has earned it and can be paid for.

        Silent about the ones that cannot: selecting the whole army and
        pressing P should promote who it can, not produce eight complaints.
        """
        promoted = 0
        blocked = ""
        for uid in sorted(self.selected):
            unit = self.view.units.get(uid)
            if unit is None or unit["owner"] != self.my_pid:
                continue
            if any(o["o"] == "promote" and o["uid"] == uid for o in self.queued):
                continue
            rank = unit.get("rank", 0)
            price = promotion_cost(rank)
            if not price:
                blocked = blocked or "already at the top rank"
                continue
            if not unit.get("blooded"):
                blocked = blocked or "not been in a fight yet"
                continue
            if self._spent() + price > self.view.supply:
                blocked = blocked or "not enough supply"
                continue
            self.queued.append({"o": "promote", "uid": uid})
            promoted += 1
        if promoted:
            self.status = f"Promoted {promoted}"
            self.sfx.play("order")
            self.ready_sent = False
        elif blocked:
            self._complain(f"No promotions: {blocked}")
            self.sfx.play("deny")

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
        # Drop that Engineer from the selection. It stays selected otherwise,
        # and the next right-click -- repositioning, or a select-all army move
        # -- would silently cancel the build you just paid for.
        self.selected.discard(chosen["uid"])
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
            elif order["o"] == "airstrike":
                total += AIRSTRIKE_COST
            elif order["o"] == "promote":
                unit = self.view.units.get(order["uid"])
                total += promotion_cost(unit.get("rank", 0)) if unit else 0
            elif order["o"] == "gift":
                total += int(order.get("supply", 0))
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
        if self.mode == "game" and self.parley:
            self._draw_parley()
        else:
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
        # Everything that lives in world coordinates is clipped to the board
        # viewport. On a scrolling map a unit near the edge, a tracer, or a
        # drifting damage number is otherwise perfectly happy to draw itself
        # across the command panel.
        self.screen.fill(UI_BG)
        self.screen.set_clip(Board.VIEW)
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
        self.screen.set_clip(None)
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
            elif order["o"] == "airstrike":
                self._draw_blast(tuple(order["to"]))
        # Every Field Hospital's ward, so it is obvious which tiles heal --
        # the rule is a radius and a radius you cannot see is a rule you have
        # to learn by accident.
        for building in self.view.buildings.values():
            info = BUILDING.get(building["code"])
            if (info is not None and info.heal and not building.get("under")
                    and building["owner"] == self.my_pid):
                for tile in self._ring((building["x"], building["y"]),
                                       info.heal_radius):
                    self.renderer.highlight(self.screen, tile, UI_GOOD)
        if self.aiming is not None:
            tile = board.to_tile(self.mouse)
            if tile is not None:
                self._draw_blast(tile)
        if self.placing:
            tile = board.to_tile(self.mouse)
            if tile is not None:
                ok = board.map.passable(*tile)
                self.renderer.highlight(self.screen, tile,
                                        UI_GOOD if ok else UI_WARN)
        if self.drag_anchor is not None:
            self.renderer.draw_selection_box(self.screen, self.drag_anchor,
                                             self.mouse)

    def _draw_blast(self, centre) -> None:
        """The tiles an airstrike will cover, so nobody bombs their own line."""
        for tile in self._ring(centre, AIRSTRIKE_RADIUS):
            friendly = self.view.unit_at(tile)
            mine = friendly is not None and friendly["owner"] == self.my_pid
            self.renderer.highlight(self.screen, tile,
                                    UI_WARN if mine else UI_ACCENT)

    def _ring(self, centre, radius: int) -> list:
        board = self.renderer.board
        out = []
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                tile = (centre[0] + dx, centre[1] + dy)
                if board.map.inside(*tile):
                    out.append(tile)
        return out

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
                if building.get("stalled"):
                    lines.append(("No Engineer working here", UI_WARN, 14))
                    lines.append(("Send any Engineer alongside to finish it",
                                  UI_DIM, 13))
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

        # The minimap band is reserved first, so no panel state can draw into
        # it. Everything above is given the shortened panel to work with.
        content = pygame.Rect(panel.x, panel.y, panel.width,
                              panel.height - MINIMAP_BAND)
        if self.selected_building is not None:
            self._draw_production(content, x, y)
        elif self._selected_workers():
            self._draw_engineering(content, x, y)
        elif self.selected:
            self._draw_selection(content, x, y)
        else:
            self._draw_overview(content, x, y)
        self._draw_minimap()

    def _draw_minimap(self) -> None:
        rect = self._minimap_rect()
        if rect is None:
            return
        self.renderer.draw_minimap(self.screen, rect, self.view, self.colors,
                                   self.my_pid)
        hint = ("click to look there" if self.renderer.board.scrolls
                else "whole map in view")
        ui.draw_text(self.screen, hint, rect.centerx, rect.top - 11, 11,
                     UI_DIM, anchor="midtop")
        self._tips.append((rect, [
            ("Minimap", UI_ACCENT, 17),
            ("Click anywhere to move the camera there.", UI_TEXT, 14),
            ("Arrows or WASD scroll; Home returns to your", UI_TEXT, 13),
            ("Command Post. The box is what you can see.", UI_TEXT, 13),
        ]))

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
            ui.draw_text(self.screen, "a move order cancels it", x, y + 12, 12,
                         UI_DIM)
            y += 12
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
        if info.heal:
            lines.append((f"Heals {info.heal} hp a turn, within "
                          f"{info.heal_radius} tile"
                          f"{'s' if info.heal_radius != 1 else ''}", UI_GOOD, 14))
            lines.append((f"{MEDIC_HEAL_COST} supply per point of health",
                          UI_TEXT, 14))
            lines.append(("Patients must be told to hold, and cannot shoot",
                          UI_WARN, 14))
        if info.airstrikes:
            lines.append((f"One {AIRSTRIKE_COST}-supply airstrike a turn",
                          UI_GOOD, 14))
            lines.append(("Lands mid-turn and hits everyone underneath",
                          UI_WARN, 14))
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
        if btype.airstrikes:
            queued = any(o["o"] == "airstrike" and o["bid"] == building["bid"]
                         for o in self.queued)
            affordable = (not queued
                          and self._spent() + AIRSTRIKE_COST <= self.view.supply)
            rect = pygame.Rect(x, y, PANEL_W - 16, 26)
            ui.draw_panel(self.screen, rect,
                          UI_PANEL_HI if affordable else shade(UI_PANEL, 0.8))
            ui.draw_text(self.screen, "Call airstrike" if not queued
                         else "Strike called", rect.x + 5, rect.y + 2, 15,
                         UI_TEXT if affordable else UI_DIM)
            ui.draw_text(self.screen, f"{AIRSTRIKE_COST}s", rect.right - 5,
                         rect.y + 2, 13, UI_DIM, anchor="topright")
            ui.draw_text(self.screen, "one a turn, anywhere on the map",
                         rect.x + 5, rect.y + 14, 11, UI_DIM)
            self._button(rect, "", "airstrike", hidden=True, enabled=affordable)
            self._tips.append((rect, [
                ("Airstrike", UI_ACCENT, 17),
                (f"{AIRSTRIKE_COST} supply, one per Airfield per turn", UI_TEXT, 14),
                ("Lands halfway through the turn, so aim where you think",
                 UI_TEXT, 13),
                ("they will be -- not where they are now.", UI_TEXT, 13),
                ("Hits everything underneath. Your troops included.",
                 UI_WARN, 14),
            ]))
            y += 30

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
        y = self._draw_promotions(x, y, units) if units else y
        y = max(y, panel.y + 120)
        ui.draw_text(self.screen, "right-click: march there", x, y, 13, UI_DIM)
        ui.draw_text(self.screen, "shift+right: advance, ready to fight",
                     x, y + 13, 13, UI_DIM)
        ui.draw_text(self.screen, "  -- but a quarter slower", x, y + 26, 12,
                     UI_WARN)
        ui.draw_text(self.screen, "tab: select whole army", x, y + 40, 13, UI_DIM)

    def _draw_promotions(self, x, y, units) -> int:
        """Ranks held by the selection, and the button to buy the next one."""
        ranked = [u for u in units if u.get("rank")]
        if ranked:
            tally: dict[int, int] = {}
            for unit in ranked:
                tally[unit["rank"]] = tally.get(unit["rank"], 0) + 1
            for rank, count in sorted(tally.items(), reverse=True):
                ui.draw_text(self.screen, f"{count}x {rank_name(rank)}", x, y,
                             14, UI_GOOD)
                y += 15
            y += 2
        eligible = [u for u in units
                    if u.get("blooded") and promotion_cost(u.get("rank", 0))]
        if not eligible:
            return y
        price = min(promotion_cost(u.get("rank", 0)) for u in eligible)
        affordable = self._spent() + price <= self.view.supply
        rect = pygame.Rect(x, y, PANEL_W - 16, 24)
        ui.draw_panel(self.screen, rect,
                      UI_PANEL_HI if affordable else shade(UI_PANEL, 0.8))
        ui.draw_text(self.screen, f"Promote ({len(eligible)})  [P]",
                     rect.x + 5, rect.y + 4, 15,
                     UI_TEXT if affordable else UI_DIM)
        ui.draw_text(self.screen, f"from {price}s", rect.right - 5, rect.y + 4,
                     13, UI_DIM, anchor="topright")
        self._button(rect, "", "promote", hidden=True, enabled=affordable)
        self._tips.append((rect, [
            ("Promotion", UI_ACCENT, 17),
            ("+1 attack and +4 health per rank, and a full heal.", UI_TEXT, 14),
            ("A Lieutenant also lends +1 attack to everyone nearby.",
             UI_GOOD, 14),
            ("Only units that have fought since their last promotion.",
             UI_TEXT, 13),
            ("Costs rise steeply: 12, then 30, then 60.", UI_DIM, 13),
        ]))
        return y + 28

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

        at_war = sum(1 for row in (self.standings or [])
                     if row.get("alive") and row["pid"] != self.my_pid
                     and self.view.pact_with(self.my_pid, row["pid"]) == "war")
        ui.draw_text(self.screen, "TABLE", 418, bar.y + 4, 12, UI_DIM)
        ui.draw_text(self.screen, f"{at_war} at war  [G]", 418, bar.y + 15, 14,
                     UI_WARN if at_war else UI_GOOD)

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
