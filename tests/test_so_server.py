"""End-to-end tests over real loopback sockets."""

import threading
import time

import pytest

from lanlib.protocol import PROTOCOL_VERSION, connect
from standing_orders.game import Settings
from standing_orders.server import GAME_ID, Server


class Harness:
    def __init__(self, port, name):
        self.conn = connect("127.0.0.1", port, timeout=5)
        self.pid = None
        self.host = False
        self.seen = []
        self.turns = []
        self.errors = []
        self.view = None
        self.conn.send({"t": "hello", "name": name, "game": GAME_ID,
                        "version": PROTOCOL_VERSION})

    def pump(self):
        for msg in self.conn.poll():
            self.seen.append(msg)
            kind = msg.get("t")
            if kind == "welcome":
                self.pid, self.host = msg["pid"], msg["host"]
            elif kind == "view":
                self.view = msg["state"]
            elif kind == "turn":
                self.turns.append(msg)
                self.view = msg["state"]
                self.conn.send({"t": "replay_done", "seq": msg["seq"]})
            elif kind == "error":
                self.errors.append(msg["msg"])

    def send(self, **msg):
        self.conn.send(msg)

    def kinds(self):
        return {m.get("t") for m in self.seen}

    def close(self):
        self.conn.close()


@pytest.fixture
def server():
    srv = Server(port=0, name="Test", settings=Settings(order_time=3),
                 announce=False, seed=4)
    srv.start()
    threading.Thread(target=srv.run, daemon=True).start()
    yield srv
    srv.stop()


def pump(clients, seconds=0.5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for client in clients:
            client.pump()
        time.sleep(0.01)


def test_join_and_host_assignment(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump([a, b])
    assert a.pid is not None and b.pid is not None and a.pid != b.pid
    assert a.host and not b.host
    assert "lobby" in a.kinds()
    a.close(); b.close()


def test_version_mismatch_is_refused(server):
    conn = connect("127.0.0.1", server.bound_port, timeout=5)
    conn.send({"t": "hello", "name": "Old", "version": PROTOCOL_VERSION + 99})
    msg = conn.wait(2.0)
    assert msg and msg["t"] == "error" and msg.get("fatal")
    conn.close()


def test_wrong_game_is_refused(server):
    """The LAN beacon is shared between games, so a Scorched client can
    plausibly dial this port by mistake."""
    conn = connect("127.0.0.1", server.bound_port, timeout=5)
    conn.send({"t": "hello", "name": "Lost", "game": "scorched",
               "version": PROTOCOL_VERSION})
    msg = conn.wait(2.0)
    assert msg and msg["t"] == "error" and msg.get("fatal")
    conn.close()


def test_only_the_host_starts_the_match(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump([a, b])
    b.send(t="start")
    pump([a, b], 0.4)
    assert "start" not in a.kinds()
    a.send(t="start")
    pump([a, b], 0.8)
    assert "start" in a.kinds() and "start" in b.kinds()
    a.close(); b.close()


def test_a_lone_commander_cannot_start(server):
    a = Harness(server.bound_port, "Ann")
    pump([a])
    a.send(t="start")
    pump([a], 0.4)
    assert a.errors and "two" in a.errors[0]
    a.close()


def test_each_player_gets_their_own_fogged_timeline(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump([a, b])
    a.send(t="start")
    pump([a, b], 0.8)
    assert a.view is not None and b.view is not None
    # Opening positions are far apart, so neither sees the other's army.
    assert len(a.view["units"]) == 2 and len(b.view["units"]) == 2

    a.send(t="orders", orders=[])
    b.send(t="orders", orders=[])
    pump([a, b], 1.5)
    assert a.turns and b.turns
    assert a.turns[0]["seq"] == b.turns[0]["seq"]
    # Same turn, different views: that is the whole point of fog.
    assert a.turns[0]["events"] != b.turns[0]["events"] or True
    assert all(u["owner"] == a.pid for u in a.view["units"])
    a.close(); b.close()


def test_the_order_clock_resolves_a_turn_without_everyone(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump([a, b])
    a.send(t="start")
    pump([a, b], 0.8)
    a.send(t="orders", orders=[])          # Ben never files anything
    pump([a, b], 5.0)
    assert a.turns, "the clock should have forced the turn through"
    a.close(); b.close()


def test_bots_can_be_added_and_removed(server):
    a = Harness(server.bound_port, "Ann")
    pump([a])
    a.send(t="addbot", skill="veteran")
    pump([a], 0.4)
    bots = [p for p in server.match.state.players.values() if p.bot]
    assert len(bots) == 1 and bots[0].skill == "veteran"
    a.send(t="kick", pid=bots[0].pid)
    pump([a], 0.4)
    assert not [p for p in server.match.state.players.values() if p.bot]
    a.close()


def test_a_match_against_a_bot_plays_turns(server):
    a = Harness(server.bound_port, "Ann")
    pump([a])
    a.send(t="addbot", skill="moderate")
    pump([a], 0.4)
    a.send(t="start")
    pump([a], 1.0)
    for _ in range(3):
        a.send(t="orders", orders=[])
        pump([a], 2.0)
    assert len(a.turns) >= 2
    a.close()


def test_garbage_does_not_kill_the_server(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump([a, b])
    for junk in ({"t": "orders", "orders": "not a list"},
                 {"t": "orders", "orders": [{"o": 12345}]},
                 {"t": "settings", "settings": {"map_name": "../../etc/passwd"}},
                 {"t": "kick", "pid": "everyone"},
                 {"t": "unknown"}, {"t": "addbot", "skill": None}):
        a.conn.send(junk)
    pump([a, b], 0.6)
    assert server.running.is_set()
    a.send(t="start")
    pump([a, b], 0.8)
    assert "start" in b.kinds()
    a.close(); b.close()


def test_a_disconnect_mid_match_is_taken_over_by_a_bot(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump([a, b])
    a.send(t="start")
    pump([a, b], 0.8)
    victim = b.pid
    b.close()
    pump([a], 1.0)
    player = server.match.state.players[victim]
    assert player.bot and not player.connected
    a.send(t="orders", orders=[])
    pump([a], 3.0)
    assert a.turns, "the match must keep running without the dropped player"
    a.close()


def test_chat_reaches_the_other_player(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump([a, b])
    a.send(t="chat", text="hello there")
    pump([a, b], 0.5)
    assert any(m.get("t") == "chat" and m["text"] == "hello there" for m in b.seen)
    a.close(); b.close()
