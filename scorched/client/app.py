"""The game client: menus, lobby, play, shop.

One process, one window, a small state machine.  Hosting spawns the authoritative
server on a background thread inside this same process and then connects to it
over loopback, so "Host Game" is a button rather than a second terminal.
"""

from __future__ import annotations

import math
import os
import socket
import threading
import time

import pygame

from lanlib import discovery
from ..game import COLOR_NAMES, MAX_PLAYERS, Settings, TEAM_COLORS
from lanlib.protocol import DEFAULT_PORT, PROTOCOL_VERSION, Connection, connect
from ..server import Server
from ..terrain import PLAY_H, STYLES, WORLD_H, WORLD_W
from ..weapons import WEAPON_BY_CODE
from lanlib import ui
from .anim import ShotPlayer
from .audio import ScorchedSfx
from .draw import Hud, Renderer, _team_color
from .palette import (UI_ACCENT, UI_BG, UI_DIM, UI_GOOD, UI_PANEL,
                      UI_PANEL_HI, UI_TEXT, UI_WARN, shade)
from .state import ClientState

FPS = 60
ANGLE_KEYS = (pygame.K_LEFT, pygame.K_RIGHT)
POWER_KEYS = (pygame.K_UP, pygame.K_DOWN)
MOVE_KEYS = (pygame.K_a, pygame.K_d)


class Repeater:
    """Key auto-repeat that accelerates while held.

    Artillery games live or die on this: nudging a power value from 500 to 780
    must be one held key, not fifty taps, but the first press must still move
    exactly one unit for fine aim.
    """

    DELAY = 0.28

    def __init__(self) -> None:
        self.held: dict[int, float] = {}
        self.next_at: dict[int, float] = {}

    def press(self, key: int, now: float) -> None:
        self.held[key] = now
        self.next_at[key] = now + self.DELAY

    def release(self, key: int) -> None:
        self.held.pop(key, None)
        self.next_at.pop(key, None)

    def clear(self) -> None:
        self.held.clear()
        self.next_at.clear()

    def steps(self, key: int, now: float) -> int:
        """How many units this key should apply this frame."""
        started = self.held.get(key)
        if started is None:
            return 0
        if now < self.next_at.get(key, 0.0):
            return 0
        elapsed = now - started - self.DELAY
        interval = max(0.012, 0.06 - elapsed * 0.05)
        self.next_at[key] = now + interval
        return 1 if elapsed < 0.8 else (3 if elapsed < 2.2 else 8)


class App:
    def __init__(self, name: str = "", fullscreen: bool = False,
                 sound: bool = True, scaled: bool = True) -> None:
        pygame.init()
        pygame.display.set_caption("Scorched - LAN Artillery")
        # SCALED lets SDL stretch the 640x400 playfield to the window, on the
        # GPU where one is available. On a Pi whose SDL falls back to software
        # scaling that stretch can cost more than the whole game does, so
        # --no-scale opens a plain unscaled window instead.
        flags = pygame.RESIZABLE
        if scaled:
            flags |= pygame.SCALED
        if fullscreen:
            flags |= pygame.FULLSCREEN
        self.screen = self._open_display(flags)
        self.clock = pygame.time.Clock()
        self.sfx = ScorchedSfx(enabled=sound)
        self.renderer = Renderer()
        self.hud = Hud()
        self.state = ClientState()
        self.running = True
        self.mode = "menu"
        self.conn: Connection | None = None
        self.server: Server | None = None
        self.server_thread: threading.Thread | None = None
        self.player_name = name or _default_name()
        self.status = ""
        self.error = ""
        self.shot: ShotPlayer | None = None
        self.repeat = Repeater()
        self.chat_input: ui.TextInput | None = None
        self.mouse = (0, 0)
        self._aim_dirty = 0.0
        self._buttons: list[ui.Button] = []
        self._servers: list[dict] = []
        self._scanning = False
        self._addr_input = ui.TextInput((0, 0, 200, 22), "", 40)
        self._name_input = ui.TextInput((0, 0, 200, 22), self.player_name, 14)
        self._shop_scroll = 0
        self._lobby_settings = Settings()
        self._title_seed = int(time.time()) & 0xFFFF
        self._menu_terrain_built = False

    @staticmethod
    def _open_display(flags: int) -> pygame.Surface:
        """Open the window, degrading gracefully rather than refusing to run."""
        for attempt_flags, vsync in ((flags, 1), (flags, 0),
                                     (flags & ~pygame.SCALED, 0)):
            try:
                return pygame.display.set_mode((WORLD_W, WORLD_H), attempt_flags,
                                               vsync=vsync)
            except pygame.error:
                continue
        return pygame.display.set_mode((WORLD_W, WORLD_H))

    # =====================================================================
    # main loop
    # =====================================================================
    def run(self) -> None:
        while self.running:
            self.clock.tick(FPS)
            self.mouse = _logical_mouse()
            self._handle_events()
            self._pump_network()
            self._update()
            self._draw()
            pygame.display.flip()
        self._teardown()

    def _teardown(self) -> None:
        if self.conn:
            self.conn.close()
        if self.server:
            self.server.stop()
        pygame.quit()

    # =====================================================================
    # networking
    # =====================================================================
    def host_game(self, settings: Settings, bots: int, skill: str) -> None:
        self.disconnect()
        try:
            self.server = Server(port=DEFAULT_PORT, name=f"{self.player_name}'s game",
                                 settings=settings, bots=bots, bot_skill=skill)
            self.server.start()
        except OSError as exc:
            self.error = f"Could not host: {exc}"
            self.server = None
            return
        self.server_thread = threading.Thread(target=self.server.run,
                                              name="server", daemon=True)
        self.server_thread.start()
        self.join_game("127.0.0.1", self.server.bound_port)

    def join_game(self, host: str, port: int = DEFAULT_PORT) -> None:
        self.error = ""
        self.status = f"Connecting to {host}..."
        self._draw()
        pygame.display.flip()
        try:
            self.conn = connect(host, port, timeout=6.0)
        except (ConnectionError, OSError, socket.gaierror) as exc:
            self.error = f"Could not connect: {exc}"
            self.status = ""
            return
        self.state = ClientState()
        self.conn.send({"t": "hello", "name": self.player_name,
                        "version": PROTOCOL_VERSION})
        self.status = ""
        self.mode = "lobby"

    def disconnect(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None
        if self.server:
            self.server.stop()
            self.server = None
        self.shot = None
        self.state = ClientState()

    def send(self, msg: dict) -> None:
        if self.conn:
            self.conn.send(msg)

    def _pump_network(self) -> None:
        if not self.conn:
            return
        for msg in self.conn.poll():
            try:
                self._on_message(msg)
            except Exception as exc:
                self.state.note(f"bad message: {exc!r}")

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("t")
        state = self.state

        if kind == "__closed__":
            self.error = f"Disconnected: {msg.get('reason', 'connection lost')}"
            self.disconnect()
            self.mode = "menu"

        elif kind == "welcome":
            state.my_pid = msg["pid"]
            state.is_host = msg.get("host", False)
            state.settings = msg.get("settings", {})
            state.catalogue = msg.get("catalogue", [])
            state.colors = msg.get("colors", list(COLOR_NAMES))
            state.server_name = msg.get("server", "")
            self._lobby_settings = Settings.from_wire(state.settings)

        elif kind == "host":
            state.is_host = msg.get("host", False)

        elif kind == "lobby":
            state.sync_players(msg.get("players", []))
            state.settings = msg.get("settings", state.settings)
            self._lobby_settings = Settings.from_wire(state.settings)
            state.is_host = (msg.get("host", -1) == state.my_pid)
            if msg.get("phase") == "lobby" and self.mode not in ("lobby",):
                self.mode = "lobby"

        elif kind == "round":
            self._begin_round(msg)

        elif kind == "turn":
            state.turn_pid = msg["pid"]
            state.wind = msg.get("wind", state.wind)
            state.phase = "aim"
            self.shot = None
            self.repeat.clear()
            if state.turn_pid == state.my_pid:
                self.sfx.play("turn")
                self.hud.flash("Your turn", 1.6)
            else:
                who = state.players.get(state.turn_pid, {}).get("name", "?")
                self.hud.flash(f"{who} is aiming", 1.4)

        elif kind == "state":
            state.phase = msg.get("phase", state.phase)
            state.wind = msg.get("wind", state.wind)
            state.round = msg.get("round", state.round)
            state.rounds = msg.get("rounds", state.rounds)
            state.time_left = msg.get("time", -1)
            turn = msg.get("turn", -1)
            if turn >= 0:
                state.turn_pid = turn
            # While a shot plays, the animation owns hp/position; adopting the
            # server's post-shot values early would teleport tanks mid-flight.
            self._merge_players(msg.get("players", []), soft=self.shot is not None)
            if state.phase == "buy" and self.mode != "shop":
                self.mode = "shop"
            elif state.phase in ("aim", "resolve") and self.mode not in ("game", "pause"):
                self.mode = "game"

        elif kind == "aim":
            player = state.players.get(msg["pid"])
            if player is not None and msg["pid"] != state.my_pid:
                player["angle"] = msg["angle"]
                player["power"] = msg["power"]
                player["weapon"] = msg.get("weapon", player.get("weapon", "bmis"))

        elif kind == "moved":
            player = state.players.get(msg["pid"])
            if player is not None:
                player["x"], player["y"] = msg["x"], msg["y"]
                player["fuel"] = msg.get("fuel", player.get("fuel", 0))
                self.sfx.play("move")

        elif kind == "shieldup":
            player = state.players.get(msg["pid"])
            if player is not None:
                player["shield"] = msg["sp"]
                player["shield_max"] = max(msg["sp"], player.get("shield_max", 0))
            self.sfx.play("shield")

        elif kind == "shot":
            state.phase = "resolve"
            self.mode = "game"
            self.shot = ShotPlayer(msg, state, self.renderer, self.sfx)

        elif kind == "shop":
            state.catalogue = msg.get("catalogue", state.catalogue)
            state.standings = msg.get("standings", [])
            state.phase = "buy"
            self.mode = "shop"
            self._shop_scroll = 0

        elif kind == "you":
            player = msg.get("player")
            if player:
                state.players[player["pid"]] = player

        elif kind == "gameover":
            state.standings = msg.get("standings", [])
            state.phase = "gameover"
            self.mode = "gameover"
            self.sfx.play("win")

        elif kind == "log":
            for line in msg.get("lines", []):
                if line not in state.log:
                    state.note(line)

        elif kind == "chat":
            color = _team_color(state.players.get(msg["pid"], {}).get("color", 0))
            state.chat.append((msg.get("name", "?"), msg.get("text", ""), color))
            del state.chat[:-6]

        elif kind == "error":
            self.error = msg.get("msg", "error")
            self.hud.flash(self.error, 3.0)
            self.sfx.play("deny")
            if msg.get("fatal"):
                self.disconnect()
                self.mode = "menu"

    def _merge_players(self, rows: list, soft: bool) -> None:
        """Apply a server player snapshot.

        ``soft`` skips the fields an in-flight animation is currently driving,
        so authority still lands -- just one shot later, when it cannot look
        like a glitch.
        """
        volatile = ("hp", "x", "y", "alive", "shield")
        for row in rows:
            pid = row["pid"]
            existing = self.state.players.get(pid)
            if existing is None:
                self.state.players[pid] = dict(row)
                continue
            mine = pid == self.state.my_pid
            for key, value in row.items():
                if soft and key in volatile:
                    continue
                if mine and key in ("angle", "power"):
                    # My own barrel is driven by local input between "aim"
                    # sends. The server's copy always lags live key-repeat by
                    # at least one round trip, so applying it here would snap
                    # the barrel backward mid-adjustment -- my own outgoing
                    # "aim" messages are what keep the server in sync, not
                    # this heartbeat.
                    continue
                existing[key] = value

    def _begin_round(self, msg: dict) -> None:
        from ..terrain import Terrain
        state = self.state
        state.terrain = Terrain.from_wire(msg["terrain"])
        state.round = msg.get("round", 1)
        state.rounds = msg.get("rounds", 1)
        state.wind = msg.get("wind", 0)
        state.settings = msg.get("settings", state.settings)
        state.sync_players(msg.get("players", []))
        state.round_seed = (state.round * 7919) ^ len(state.terrain.height)
        self.renderer.begin_round(state.terrain, state.round_seed, TEAM_COLORS)
        self.shot = None
        self.mode = "game"
        state.phase = "aim"

    # =====================================================================
    # events
    # =====================================================================
    def _handle_events(self) -> None:
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
        if event.type == pygame.MOUSEBUTTONDOWN:
            event = pygame.event.Event(event.type, pos=self.mouse, button=event.button)
        if self.chat_input.handle(event):
            text = self.chat_input.value.strip()
            if text:
                self.send({"t": "chat", "text": text})
            self.chat_input = None

    # -- menu --------------------------------------------------------------
    def _events_menu(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.running = False
        self._name_input.handle(_remap(event, self.mouse))
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    self._menu_action(button.action)

    def _menu_action(self, action: str) -> None:
        self.player_name = (self._name_input.value.strip() or "Player")[:14]
        if action == "host":
            self.mode = "hostsetup"
        elif action == "browse":
            self.mode = "browse"
            self._start_scan()
        elif action == "direct":
            self.mode = "direct"
            self._addr_input.focused = True
        elif action == "quit":
            self.running = False

    # -- host setup --------------------------------------------------------
    def _events_hostsetup(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.mode = "menu"
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    action = button.action
                    if action == "back":
                        self.mode = "menu"
                    elif action == "go":
                        self.host_game(self._lobby_settings,
                                       getattr(self, "_bot_count", 1),
                                       getattr(self, "_bot_skill", "moderate"))
                    elif action.startswith("bots"):
                        delta = 1 if action.endswith("+") else -1
                        count = getattr(self, "_bot_count", 1) + delta
                        self._bot_count = max(0, min(MAX_PLAYERS - 1, count))
                    elif action.startswith("skill"):
                        order = ("novice", "moderate", "expert", "cyborg")
                        current = order.index(getattr(self, "_bot_skill", "moderate"))
                        step = 1 if action.endswith("+") else -1
                        self._bot_skill = order[(current + step) % len(order)]
                    else:
                        self._tweak_setting(action)

    # -- browse ------------------------------------------------------------
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
                self._servers = discovery.scan(1.4)
            except OSError:
                self._servers = []
            finally:
                self._scanning = False

        threading.Thread(target=work, name="scan", daemon=True).start()

    # -- direct connect ----------------------------------------------------
    def _events_direct(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.mode = "menu"
            return
        if self._addr_input.handle(_remap(event, self.mouse)):
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
        text = self._addr_input.value.strip()
        if not text:
            return
        host, _, port_text = text.partition(":")
        port = int(port_text) if port_text.isdigit() else DEFAULT_PORT
        self.join_game(host.strip(), port)

    # -- lobby -------------------------------------------------------------
    def _events_lobby(self, event) -> None:
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.disconnect()
            self.mode = "menu"
            return
        if event.type == pygame.KEYDOWN and event.key in (pygame.K_t, pygame.K_RETURN):
            self._open_chat()
            return
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    self._lobby_action(button.action)

    def _lobby_action(self, action: str) -> None:
        me = self.state.me
        if action == "leave":
            self.disconnect()
            self.mode = "menu"
        elif action == "ready":
            self.send({"t": "setup", "ready": not (me or {}).get("ready", False)})
        elif action == "color":
            current = (me or {}).get("color", 0)
            self.send({"t": "setup", "color": (current + 1) % MAX_PLAYERS})
        elif action == "addbot":
            self.send({"t": "addbot", "skill": getattr(self, "_bot_skill", "moderate")})
        elif action.startswith("kick:"):
            self.send({"t": "kick", "pid": int(action.split(":")[1])})
        elif action == "start":
            self.send({"t": "start"})
        elif self.state.is_host:
            self._tweak_setting(action)
            self.send({"t": "settings", "settings": self._lobby_settings.to_wire()})

    def _tweak_setting(self, action: str) -> None:
        """Shared by the host-setup screen and the lobby."""
        settings = self._lobby_settings
        if ":" not in action:
            return
        field, direction = action.split(":")
        step = 1 if direction == "+" else -1
        if field == "rounds":
            settings.rounds = max(1, min(20, settings.rounds + step))
        elif field == "cash":
            settings.start_cash = max(0, min(80000, settings.start_cash + step * 2000))
        elif field == "turn":
            settings.turn_time = max(0, min(180, settings.turn_time + step * 5))
        elif field == "wind":
            settings.wind_max = max(0, min(160, settings.wind_max + step * 10))
        elif field == "gravity":
            settings.gravity = max(0.05, min(0.4, round(settings.gravity + step * 0.01, 3)))
        elif field == "terrain":
            options = list(STYLES)
            index = options.index(settings.terrain_style) if settings.terrain_style in options else 0
            settings.terrain_style = options[(index + step) % len(options)]
        elif field == "sudden":
            settings.sudden_death = max(0, min(200, settings.sudden_death + step * 5))
        elif field == "walls":
            options = ["none", "rebound", "wrap"]
            index = options.index(settings.wall_mode) if settings.wall_mode in options else 0
            settings.wall_mode = options[(index + step) % len(options)]
        settings.clamp()

    def _open_chat(self) -> None:
        self.chat_input = ui.TextInput((60, WORLD_H - 60, 520, 22), "", 100)
        self.chat_input.focused = True

    # -- game --------------------------------------------------------------
    def _events_game(self, event) -> None:
        state = self.state
        now = time.monotonic()

        if event.type == pygame.KEYDOWN:
            key = event.key
            if key == pygame.K_ESCAPE:
                self.mode = "pause"
                return
            if key in (pygame.K_t,):
                self._open_chat()
                return
            if not state.my_turn:
                return
            if key in ANGLE_KEYS or key in POWER_KEYS or key in MOVE_KEYS:
                self.repeat.press(key, now)
                self._apply_key(key, 1)
            elif key in (pygame.K_SPACE, pygame.K_RETURN):
                self._fire()
            elif key in (pygame.K_TAB, pygame.K_e):
                self._cycle_weapon(1)
            elif key == pygame.K_q:
                self._cycle_weapon(-1)
            elif key == pygame.K_s:
                self._raise_shield()
            elif pygame.K_1 <= key <= pygame.K_9:
                self._quick_weapon(key - pygame.K_1)

        elif event.type == pygame.KEYUP:
            self.repeat.release(event.key)

        elif event.type == pygame.MOUSEBUTTONDOWN and state.my_turn:
            if event.button == 1 and self.mouse[1] < PLAY_H:
                self._aim_at(self.mouse)
            elif event.button == 4:
                self._nudge_power(5)
            elif event.button == 5:
                self._nudge_power(-5)

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

    def _apply_key(self, key: int, amount: int) -> None:
        me = self.state.me
        if me is None:
            return
        if key == pygame.K_LEFT:
            self._nudge_angle(amount)
        elif key == pygame.K_RIGHT:
            self._nudge_angle(-amount)
        elif key == pygame.K_UP:
            self._nudge_power(amount)
        elif key == pygame.K_DOWN:
            self._nudge_power(-amount)
        elif key == pygame.K_a:
            self.send({"t": "move", "dir": -1, "steps": min(4, amount)})
        elif key == pygame.K_d:
            self.send({"t": "move", "dir": 1, "steps": min(4, amount)})

    def _nudge_angle(self, amount: int) -> None:
        me = self.state.me
        if me is None:
            return
        me["angle"] = max(0, min(180, me["angle"] + amount))
        self._aim_dirty = time.monotonic()

    def _nudge_power(self, amount: int) -> None:
        me = self.state.me
        if me is None:
            return
        me["power"] = max(0, min(1000, me["power"] + amount))
        self._aim_dirty = time.monotonic()

    def _aim_at(self, pos) -> None:
        """Point the barrel at wherever the player clicked."""
        me = self.state.me
        if me is None:
            return
        dx = pos[0] - me["x"]
        dy = me["y"] - 6 - pos[1]
        angle = math.degrees(math.atan2(dy, dx))
        me["angle"] = max(0, min(180, int(round(angle))))
        self._aim_dirty = time.monotonic()

    def _cycle_weapon(self, step: int) -> None:
        codes = self.state.my_weapons()
        me = self.state.me
        if me is None or not codes:
            return
        try:
            index = codes.index(me["weapon"])
        except ValueError:
            index = 0
        code = codes[(index + step) % len(codes)]
        me["weapon"] = code
        self.send({"t": "weapon", "code": code})
        self.sfx.play("select")

    def _quick_weapon(self, index: int) -> None:
        codes = self.state.my_weapons()
        if index < len(codes):
            me = self.state.me
            if me is not None:
                me["weapon"] = codes[index]
                self.send({"t": "weapon", "code": codes[index]})
                self.sfx.play("select")

    def _raise_shield(self) -> None:
        items = self.state.my_items()
        for code in ("hshld", "shld"):
            if items.get(code, 0) > 0:
                self.send({"t": "shield", "code": code})
                return
        self.hud.flash("No shields in stock")
        self.sfx.play("deny")

    def _fire(self) -> None:
        me = self.state.me
        if me is None:
            return
        self.send({"t": "aim", "angle": me["angle"], "power": me["power"]})
        self.send({"t": "fire"})
        self.state.phase = "resolve"

    # -- shop --------------------------------------------------------------
    def _events_shop(self, event) -> None:
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                self.send({"t": "buydone"})
            elif event.key == pygame.K_t:
                self._open_chat()
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button in (4, 5):
                self._shop_scroll = max(0, self._shop_scroll + (1 if event.button == 5 else -1))
                return
            for button in self._buttons:
                if button.clicked(self.mouse):
                    action = button.action
                    if action == "done":
                        self.sfx.play("click")
                        self.send({"t": "buydone"})
                    elif action.startswith("buy:"):
                        code = action.split(":")[1]
                        qty = 5 if pygame.key.get_mods() & pygame.KMOD_SHIFT else 1
                        if event.button == 3:
                            self.send({"t": "sell", "code": code})
                        else:
                            self.send({"t": "buy", "code": code, "qty": qty})
                        self.sfx.play("select")

    def _events_gameover(self, event) -> None:
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for button in self._buttons:
                if button.clicked(self.mouse):
                    self.sfx.play("click")
                    if button.action == "again":
                        self.send({"t": "restart"})
                    elif button.action == "leave":
                        self.disconnect()
                        self.mode = "menu"
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            self.disconnect()
            self.mode = "menu"

    # =====================================================================
    # update
    # =====================================================================
    def _update(self) -> None:
        now = time.monotonic()
        if self.mode == "game" and self.state.my_turn:
            for key in list(self.repeat.held):
                steps = self.repeat.steps(key, now)
                if steps:
                    self._apply_key(key, steps)
            # Mirror aim to the server at a modest rate: enough for spectators
            # to see the barrel sweep, nowhere near enough to matter for
            # bandwidth on a domestic LAN.
            if self._aim_dirty and now - self._aim_dirty > 0.05:
                me = self.state.me
                if me is not None:
                    self.send({"t": "aim", "angle": me["angle"], "power": me["power"]})
                self._aim_dirty = 0.0

        if self.shot is not None:
            if not self.shot.update():
                self.send({"t": "anim_done", "seq": self.shot.seq})
                self.shot = None

        if self.state.time_left > 0:
            self.state.time_left = max(0.0, self.state.time_left - 1.0 / FPS)

    # =====================================================================
    # drawing
    # =====================================================================
    def _draw(self) -> None:
        self._buttons = []
        painter = getattr(self, f"_draw_{self.mode}", self._draw_menu)
        painter()
        if self.chat_input is not None:
            ui.draw_text(self.screen, "SAY:", 26, WORLD_H - 56, 16, UI_ACCENT)
            self.chat_input.draw(self.screen)
        for button in self._buttons:
            button.update(self.mouse)
            button.draw(self.screen)
        if self.error:
            ui.draw_text(self.screen, self.error, WORLD_W // 2, WORLD_H - 12, 15,
                         UI_WARN, anchor="center")

    def _button(self, rect, label, action, size=18, enabled=True,
                hidden=False) -> ui.Button:
        button = ui.Button(rect, label, action, size, enabled, hidden)
        self._buttons.append(button)
        return button

    # -- menu --------------------------------------------------------------
    def _draw_menu(self) -> None:
        self._draw_title_backdrop()
        title = "SCORCHED"
        ui.draw_text(self.screen, title, WORLD_W // 2 + 2, 56, 64, (12, 10, 16),
                     anchor="center")
        ui.draw_text(self.screen, title, WORLD_W // 2, 54, 64, UI_ACCENT,
                     anchor="center")
        ui.draw_text(self.screen, "LAN ARTILLERY FOR STUBBORN FRIENDS",
                     WORLD_W // 2, 88, 17, UI_DIM, anchor="center")

        self._name_input.rect = pygame.Rect(WORLD_W // 2 - 100, 128, 200, 22)
        self._name_input.draw(self.screen, "CALLSIGN")

        y = 178
        for label, action in (("Host a Game", "host"),
                              ("Find LAN Games", "browse"),
                              ("Connect by Address", "direct"),
                              ("Quit", "quit")):
            self._button((WORLD_W // 2 - 100, y, 200, 28), label, action)
            y += 36

        ui.draw_text(self.screen, "F11 fullscreen  ·  arrows aim  ·  space fires",
                     WORLD_W // 2, WORLD_H - 34, 15, UI_DIM, anchor="center")
        if self.status:
            ui.draw_text(self.screen, self.status, WORLD_W // 2, WORLD_H - 52, 16,
                         UI_TEXT, anchor="center")

    def _draw_title_backdrop(self) -> None:
        if not self._menu_terrain_built:
            from ..terrain import Terrain
            terrain = Terrain.generate(self._title_seed, "mountains")
            self.renderer.begin_round(terrain, self._title_seed, TEAM_COLORS)
            self._menu_terrain_built = True
        self.renderer.draw_world(self.screen)
        self.screen.fill(UI_BG, (0, PLAY_H, WORLD_W, WORLD_H - PLAY_H))
        veil = pygame.Surface((WORLD_W, PLAY_H), pygame.SRCALPHA)
        veil.fill((10, 10, 18, 150))
        self.screen.blit(veil, (0, 0))

    # -- host setup ---------------------------------------------------------
    def _draw_hostsetup(self) -> None:
        self._draw_title_backdrop()
        ui.draw_text(self.screen, "HOST A GAME", WORLD_W // 2, 16, 30, UI_ACCENT,
                     anchor="center")
        panel = pygame.Rect(80, 46, WORLD_W - 160, 286)
        ui.draw_panel(self.screen, panel)

        bots = getattr(self, "_bot_count", 1)
        skill = getattr(self, "_bot_skill", "moderate")
        settings = self._lobby_settings
        rows = [
            ("Computer players", str(bots), "bots"),
            ("Bot skill", skill.title(), "skill"),
            ("Rounds", str(settings.rounds), "rounds"),
            ("Starting cash", f"${settings.start_cash:,}", "cash"),
            ("Turn seconds", str(settings.turn_time or "off"), "turn"),
            ("Max wind", str(settings.wind_max), "wind"),
            ("Gravity", f"{settings.gravity:.2f}", "gravity"),
            ("Landscape", settings.terrain_style.title(), "terrain"),
            ("Side walls", settings.wall_mode.title(), "walls"),
            ("Sudden death after", str(settings.sudden_death or "never"), "sudden"),
        ]
        y = panel.y + 12
        for label, value, field in rows:
            ui.draw_text(self.screen, label, panel.x + 16, y + 3, 17, UI_TEXT)
            self._button((panel.right - 128, y, 20, 20), "<", f"{field}:-", 18)
            ui.draw_text(self.screen, value, panel.right - 74, y + 3, 17,
                         UI_ACCENT, anchor="midtop")
            self._button((panel.right - 36, y, 20, 20), ">", f"{field}:+", 18)
            y += 27

        self._button((WORLD_W // 2 - 150, 344, 140, 28), "Back", "back")
        self._button((WORLD_W // 2 + 10, 344, 140, 28), "Start Hosting", "go")

    # -- browse -------------------------------------------------------------
    def _draw_browse(self) -> None:
        self._draw_title_backdrop()
        ui.draw_text(self.screen, "GAMES ON THIS NETWORK", WORLD_W // 2, 24, 28,
                     UI_ACCENT, anchor="center")
        panel = pygame.Rect(60, 60, WORLD_W - 120, 250)
        ui.draw_panel(self.screen, panel)

        if self._scanning:
            dots = "." * (1 + int(time.time() * 3) % 3)
            ui.draw_text(self.screen, f"Searching{dots}", panel.centerx,
                         panel.centery, 20, UI_DIM, anchor="center")
        elif not self._servers:
            ui.draw_text(self.screen, "No games found.", panel.centerx,
                         panel.centery - 12, 20, UI_DIM, anchor="center")
            ui.draw_text(self.screen,
                         "Make sure the host has started, and that both machines",
                         panel.centerx, panel.centery + 10, 15, UI_DIM, anchor="center")
            ui.draw_text(self.screen, "are on the same network.",
                         panel.centerx, panel.centery + 24, 15, UI_DIM, anchor="center")
        else:
            y = panel.y + 10
            for info in self._servers[:7]:
                host = info.get("host", "?")
                port = int(info.get("port", DEFAULT_PORT))
                name = str(info.get("name", "Scorched"))[:26]
                players = info.get("players", 0)
                phase = info.get("phase", "lobby")
                joinable = phase == "lobby"
                ui.draw_text(self.screen, name, panel.x + 12, y + 5, 18, UI_TEXT)
                ui.draw_text(self.screen, f"{host}:{port}", panel.x + 12, y + 20,
                             14, UI_DIM)
                ui.draw_text(self.screen, f"{players}/{info.get('max', 8)}",
                             panel.right - 150, y + 8, 17, UI_TEXT)
                self._button((panel.right - 110, y + 4, 96, 24),
                             "Join" if joinable else "In progress",
                             f"join:{host}:{port}", 16, enabled=joinable)
                y += 34

        self._button((WORLD_W // 2 - 150, 322, 140, 28), "Back", "back")
        self._button((WORLD_W // 2 + 10, 322, 140, 28), "Search Again", "rescan")

    # -- direct -------------------------------------------------------------
    def _draw_direct(self) -> None:
        self._draw_title_backdrop()
        ui.draw_text(self.screen, "CONNECT BY ADDRESS", WORLD_W // 2, 90, 28,
                     UI_ACCENT, anchor="center")
        self._addr_input.rect = pygame.Rect(WORLD_W // 2 - 130, 150, 260, 24)
        self._addr_input.draw(self.screen, "HOST  (e.g. 192.168.1.40  or  pi400.local)")
        ui.draw_text(self.screen, f"Port defaults to {DEFAULT_PORT}; use host:port to change it.",
                     WORLD_W // 2, 186, 15, UI_DIM, anchor="center")
        self._button((WORLD_W // 2 - 150, 220, 140, 28), "Back", "back")
        self._button((WORLD_W // 2 + 10, 220, 140, 28), "Connect", "connect")

    # -- lobby --------------------------------------------------------------
    def _draw_lobby(self) -> None:
        self._draw_title_backdrop()
        state = self.state
        ui.draw_text(self.screen, state.server_name or "LOBBY", WORLD_W // 2, 12,
                     26, UI_ACCENT, anchor="center")

        if self.server is not None:
            addrs = ", ".join(discovery.local_addresses()[:2])
            ui.draw_text(self.screen,
                         f"Others join at {addrs} (port {self.server.bound_port})",
                         WORLD_W // 2, 36, 15, UI_GOOD, anchor="center")

        roster = pygame.Rect(16, 54, 290, 250)
        ui.draw_panel(self.screen, roster)
        ui.draw_text(self.screen, "PLAYERS", roster.x + 8, roster.y + 6, 16, UI_DIM)
        y = roster.y + 24
        for player in sorted(state.players.values(), key=lambda p: p["pid"]):
            color = _team_color(player["color"])
            self.screen.fill(color, (roster.x + 10, y + 4, 8, 8))
            label = player["name"]
            if player["pid"] == state.my_pid:
                label += " (you)"
            ui.draw_text(self.screen, label, roster.x + 24, y, 17, UI_TEXT)
            tag = "BOT" if player["bot"] else ("READY" if player["ready"] else "...")
            ui.draw_text(self.screen, tag, roster.right - 12, y, 15,
                         UI_GOOD if player["ready"] and not player["bot"] else UI_DIM,
                         anchor="topright")
            if state.is_host and player["bot"]:
                self._button((roster.right - 68, y - 1, 18, 16), "x",
                             f"kick:{player['pid']}", 15)
            y += 24

        rules = pygame.Rect(318, 54, WORLD_W - 334, 250)
        ui.draw_panel(self.screen, rules)
        ui.draw_text(self.screen, "MATCH RULES", rules.x + 8, rules.y + 6, 16, UI_DIM)
        settings = self._lobby_settings
        rows = [
            ("Rounds", str(settings.rounds), "rounds"),
            ("Cash", f"${settings.start_cash:,}", "cash"),
            ("Turn time", str(settings.turn_time or "off"), "turn"),
            ("Wind", str(settings.wind_max), "wind"),
            ("Gravity", f"{settings.gravity:.2f}", "gravity"),
            ("Land", settings.terrain_style.title(), "terrain"),
            ("Walls", settings.wall_mode.title(), "walls"),
        ]
        ry = rules.y + 26
        for label, value, field in rows:
            ui.draw_text(self.screen, label, rules.x + 10, ry + 2, 16, UI_TEXT)
            if state.is_host:
                self._button((rules.right - 118, ry, 18, 18), "<", f"{field}:-", 16)
                self._button((rules.right - 30, ry, 18, 18), ">", f"{field}:+", 16)
            ui.draw_text(self.screen, value, rules.right - 74, ry + 2, 16,
                         UI_ACCENT, anchor="midtop")
            ry += 25

        me = state.me or {}
        ready = me.get("ready", False)
        self._button((16, 314, 110, 26), "Leave", "leave", 17)
        self._button((132, 314, 110, 26), "Colour", "color", 17)
        self._button((248, 314, 130, 26), "Not Ready" if ready else "Ready",
                     "ready", 17)
        if state.is_host:
            self._button((384, 314, 110, 26), "Add Bot", "addbot", 17)
            can_start = len(state.players) >= 2
            self._button((500, 314, 124, 26), "START", "start", 18, can_start)

        # Two lines only, below the buttons, so chat never covers them.
        self._draw_chat(WORLD_H - 14, limit=2)

    # -- game ---------------------------------------------------------------
    def _draw_game(self) -> None:
        state = self.state
        if state.terrain is None:
            self.screen.fill(UI_BG)
            ui.draw_text(self.screen, "Waiting for the round to start...",
                         WORLD_W // 2, WORLD_H // 2, 20, UI_DIM, anchor="center")
            return

        offset = self.shot.offset() if self.shot else (0, 0)
        if offset != (0, 0):
            self.screen.fill((0, 0, 0), (0, 0, WORLD_W, PLAY_H))
            self.screen.blit(self.renderer.scene, offset)
        else:
            self.renderer.draw_world(self.screen)

        players = sorted(state.players.values(), key=lambda p: p["alive"])
        for player in players:
            self.renderer.draw_tank(
                self.screen, player,
                aiming=(player["pid"] == state.turn_pid),
                highlight=(player["pid"] == state.turn_pid and state.phase == "aim"))
        self.renderer.draw_name_tags(self.screen, players, state.turn_pid,
                                     state.my_pid)

        if state.my_turn:
            self._draw_aim_guide()

        if self.shot is not None:
            self.shot.draw(self.screen)

        view = _HudView(state)
        self.hud.draw_top(self.screen, view)
        self.hud.draw_bottom(self.screen, view)
        self._draw_weapon_rack()
        self._draw_log()
        self._draw_chat(PLAY_H - 30)

    def _draw_aim_guide(self) -> None:
        """A short dotted lead-line from the barrel.

        Deliberately short: it shows the launch direction, never the whole arc.
        Seeing where the shell would land would remove the entire game.
        """
        me = self.state.me
        if me is None:
            return
        rad = math.radians(me["angle"])
        power = me["power"] / 1000.0
        length = 18 + power * 34
        x, y = me["x"], me["y"] - 6
        for i in range(4, int(length), 4):
            px = int(x + math.cos(rad) * i)
            py = int(y - math.sin(rad) * i)
            if 0 <= px < WORLD_W and 0 <= py < PLAY_H:
                self.screen.set_at((px, py), UI_ACCENT)

    def _draw_weapon_rack(self) -> None:
        """Owned weapons along the left edge, with the current one lit."""
        state = self.state
        me = state.me
        if me is None or not state.my_turn:
            return
        codes = state.my_weapons()
        x, y = 4, 20
        for index, code in enumerate(codes[:9]):
            weapon = WEAPON_BY_CODE[code]
            selected = code == me["weapon"]
            rect = pygame.Rect(x, y, 96, 15)
            back = UI_PANEL_HI if selected else (24, 24, 34)
            panel = pygame.Surface(rect.size, pygame.SRCALPHA)
            panel.fill((*back, 205 if selected else 150))
            self.screen.blit(panel, rect.topleft)
            if selected:
                pygame.draw.rect(self.screen, UI_ACCENT, rect, 1)
            ui.draw_text(self.screen, f"{index + 1}", rect.x + 3, rect.y + 2, 13,
                         UI_DIM)
            ui.draw_text(self.screen, weapon.name[:13], rect.x + 14, rect.y + 2, 14,
                         UI_ACCENT if selected else UI_TEXT)
            ammo = state.my_ammo(code)
            shown = "--" if ammo is not None and ammo < 0 else str(ammo)
            ui.draw_text(self.screen, shown, rect.right - 4, rect.y + 2, 13, UI_DIM,
                         anchor="topright")
            y += 16

    def _draw_log(self) -> None:
        y = 22
        for line in self.state.log[-4:]:
            ui.draw_text(self.screen, line, WORLD_W - 6, y, 14, UI_DIM,
                         anchor="topright", shadow=True)
            y += 13

    def _draw_chat(self, bottom: int, limit: int = 4) -> None:
        y = bottom
        for name, text, color in reversed(self.state.chat[-limit:]):
            ui.draw_text(self.screen, f"{name}: {text}", 6, y, 15, color,
                         shadow=True)
            y -= 14

    def _draw_pause(self) -> None:
        self._draw_game()
        veil = pygame.Surface((WORLD_W, WORLD_H), pygame.SRCALPHA)
        veil.fill((8, 8, 14, 190))
        self.screen.blit(veil, (0, 0))
        ui.draw_text(self.screen, "PAUSED", WORLD_W // 2, 120, 40, UI_ACCENT,
                     anchor="center")
        ui.draw_text(self.screen,
                     "The match continues without you if you wait too long.",
                     WORLD_W // 2, 158, 15, UI_DIM, anchor="center")
        self._button((WORLD_W // 2 - 150, 200, 140, 30), "Resume", "resume")
        self._button((WORLD_W // 2 + 10, 200, 140, 30), "Leave Game", "leave")
        self._draw_controls_card(pygame.Rect(WORLD_W // 2 - 160, 246, 320, 120))

    def _draw_controls_card(self, rect) -> None:
        ui.draw_panel(self.screen, rect)
        lines = [
            "Left / Right      aim the barrel",
            "Up / Down         power",
            "A / D             drive (costs fuel)",
            "1-9 / Tab / Q,E   pick a weapon",
            "S                 raise a shield",
            "Space             fire",
            "T                 chat",
        ]
        y = rect.y + 8
        for line in lines:
            ui.draw_text(self.screen, line, rect.x + 12, y, 15, UI_TEXT)
            y += 15

    # -- shop ---------------------------------------------------------------
    def _draw_shop(self) -> None:
        state = self.state
        self.screen.fill(UI_BG)
        me = state.me or {}
        ui.draw_text(self.screen, "ARMS DEALER", 16, 10, 30, UI_ACCENT)
        ui.draw_text(self.screen, f"Round {state.round + 1} of {state.rounds}",
                     16, 38, 16, UI_DIM)
        ui.draw_text(self.screen, f"${me.get('cash', 0):,}", WORLD_W - 16, 12, 30,
                     UI_GOOD, anchor="topright")
        ui.draw_text(self.screen, "left-click buy  ·  shift+click buys 5  ·  right-click sells",
                     WORLD_W - 16, 42, 14, UI_DIM, anchor="topright")

        panel = pygame.Rect(12, 60, WORLD_W - 24, 268)
        ui.draw_panel(self.screen, panel)

        ammo = me.get("inv", {}).get("ammo", {})
        items = me.get("inv", {}).get("items", {})
        cash = me.get("cash", 0)

        col_w = (panel.width - 24) // 2
        for index, row in enumerate(state.catalogue):
            col, line = divmod(index, 8)
            if col > 1:
                break
            x = panel.x + 12 + col * col_w
            y = panel.y + 8 + line * 32
            owned = ammo.get(row["code"], 0) + items.get(row["code"], 0)
            affordable = cash >= row["price"]
            rect = pygame.Rect(x, y, col_w - 12, 30)
            if rect.collidepoint(self.mouse):
                self.screen.fill(shade(UI_PANEL, 1.35), rect)
            ui.draw_text(self.screen, row["name"], x + 4, y + 1, 17,
                         UI_TEXT if affordable else UI_DIM)
            ui.draw_text(self.screen, row["blurb"][:44], x + 4, y + 16, 13, UI_DIM)
            ui.draw_text(self.screen, f"${row['price']:,}", rect.right - 60, y + 1,
                         15, UI_GOOD if affordable else UI_WARN, anchor="topright")
            ui.draw_text(self.screen, f"x{row['pack']}", rect.right - 60, y + 15,
                         13, UI_DIM, anchor="topright")
            ui.draw_text(self.screen, f"have {owned}", rect.right - 4, y + 8, 14,
                         UI_ACCENT if owned else UI_DIM, anchor="topright")
            # The row artwork above is the button; this is just its hit area.
            self._button(rect, "", f"buy:{row['code']}", hidden=True)

        waiting = [p["name"] for p in state.players.values()
                   if p.get("buying") and not p["bot"] and p["pid"] != state.my_pid]
        if waiting:
            ui.draw_text(self.screen, "Still shopping: " + ", ".join(waiting[:4]),
                         16, WORLD_H - 26, 15, UI_DIM)
        done = not (state.me or {}).get("buying", True)
        self._button((WORLD_W - 150, WORLD_H - 34, 134, 26),
                     "Waiting..." if done else "Done Shopping", "done", 17,
                     enabled=not done)

    def _draw_gameover(self) -> None:
        self._draw_title_backdrop()
        ui.draw_text(self.screen, "FINAL STANDINGS", WORLD_W // 2, 24, 32,
                     UI_ACCENT, anchor="center")
        panel = pygame.Rect(120, 62, WORLD_W - 240, 232)
        ui.draw_panel(self.screen, panel)
        y = panel.y + 12
        for place, row in enumerate(self.state.standings[:8], start=1):
            color = _team_color(row["color"])
            marker = ("1st", "2nd", "3rd")[place - 1] if place <= 3 else f"{place}th"
            ui.draw_text(self.screen, marker, panel.x + 12, y, 18,
                         UI_ACCENT if place == 1 else UI_DIM)
            self.screen.fill(color, (panel.x + 54, y + 5, 8, 8))
            ui.draw_text(self.screen, row["name"], panel.x + 70, y, 18, UI_TEXT)
            ui.draw_text(self.screen, f"{row['wins']} rounds", panel.right - 108, y,
                         16, UI_TEXT, anchor="topright")
            ui.draw_text(self.screen, f"${row['cash']:,}", panel.right - 12, y, 16,
                         UI_DIM, anchor="topright")
            y += 26
        if self.state.is_host:
            self._button((WORLD_W // 2 - 150, 306, 140, 28), "Play Again", "again")
        self._button((WORLD_W // 2 + 10, 306, 140, 28), "Leave", "leave")


class _HudView:
    """Adapter so the HUD can read the client state without importing it."""

    def __init__(self, state: ClientState) -> None:
        self.players = state.players
        self.my_pid = state.my_pid
        self.turn_pid = state.turn_pid
        self.phase = state.phase
        self.round = state.round
        self.rounds = state.rounds
        self.wind = state.wind
        self.time_left = state.time_left
        self._state = state

    def my_ammo(self, code: str):
        return self._state.my_ammo(code)


def _logical_mouse() -> tuple[int, int]:
    """Mouse position in 640x400 space, whatever the window is scaled to."""
    try:
        return pygame.mouse.get_pos()
    except pygame.error:
        return (0, 0)


def _remap(event, pos):
    """Give mouse events the logical position the UI widgets expect."""
    if event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
        return pygame.event.Event(event.type, pos=pos, button=event.button)
    return event


def _default_name() -> str:
    for key in ("SCORCHED_NAME", "USER", "USERNAME", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            return value[:14]
    return "Player"


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="scorched",
                                     description="Scorched — LAN artillery")
    parser.add_argument("--name", default="", help="your callsign")
    parser.add_argument("--connect", default="", metavar="HOST[:PORT]",
                        help="join this server immediately")
    parser.add_argument("--host", action="store_true",
                        help="start hosting immediately")
    parser.add_argument("--bots", type=int, default=1)
    parser.add_argument("--skill", default="moderate")
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--no-sound", action="store_true")
    parser.add_argument("--no-scale", action="store_true",
                        help="plain unscaled 640x400 window (fastest fallback)")
    args = parser.parse_args(argv)

    app = App(name=args.name, fullscreen=args.fullscreen,
              sound=not args.no_sound, scaled=not args.no_scale)
    app._bot_count = max(0, args.bots)
    app._bot_skill = args.skill
    if args.connect:
        host, _, port = args.connect.partition(":")
        app.join_game(host, int(port) if port.isdigit() else DEFAULT_PORT)
    elif args.host:
        app.host_game(Settings(), args.bots, args.skill)
    app.run()
    return 0
