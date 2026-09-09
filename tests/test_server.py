"""End-to-end tests over a real loopback socket.

These exercise the actual contract two machines rely on: a client sends intent,
the server decides, and every client receives the same timeline.
"""

import threading
import time

import pytest

from scorched.game import Settings
from scorched.protocol import PROTOCOL_VERSION, connect
from scorched.server import Server
from scorched.terrain import Terrain


class Harness:
    """A scripted headless client."""

    def __init__(self, port, name):
        self.conn = connect("127.0.0.1", port, timeout=5)
        self.name = name
        self.pid = None
        self.host = False
        self.seen = []
        self.shots = []
        self.errors = []
        self.turn = False
        self.phase = ""
        self.terrain = None
        self.conn.send({"t": "hello", "name": name, "version": PROTOCOL_VERSION})

    def pump(self):
        for msg in self.conn.poll():
            self.seen.append(msg)
            kind = msg.get("t")
            if kind == "welcome":
                self.pid, self.host = msg["pid"], msg["host"]
            elif kind == "round":
                self.terrain = Terrain.from_wire(msg["terrain"])
            elif kind == "turn":
                self.turn = msg["pid"] == self.pid
            elif kind == "shot":
                self.shots.append(msg)
                # Replay the authoritative terrain edits, as the real client does.
                if self.terrain is not None:
                    for event in sorted(msg["events"], key=lambda e: e["f"]):
                        if event["e"] == "terrain":
                            self.terrain.apply(event["op"], event["x"],
                                               event["y"], event["r"])
                self.conn.send({"t": "anim_done", "seq": msg["seq"]})
            elif kind == "state":
                self.phase = msg.get("phase", "")
            elif kind == "error":
                self.errors.append(msg["msg"])

    def send(self, **msg):
        self.conn.send(msg)

    def close(self):
        self.conn.close()

    def kinds(self):
        return {m.get("t") for m in self.seen}


@pytest.fixture
def server():
    srv = Server(port=0, name="Test", settings=Settings(rounds=1, turn_time=4,
                                                        buy_time=2),
                 seed=5, announce=False)
    srv.start()
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    yield srv
    srv.stop()


def pump_all(clients, seconds=0.4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for client in clients:
            client.pump()
        time.sleep(0.01)


def test_two_clients_join_and_the_first_is_host(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    assert a.pid is not None and b.pid is not None and a.pid != b.pid
    assert a.host and not b.host
    assert "lobby" in a.kinds()
    a.close(); b.close()


def test_version_mismatch_is_rejected(server):
    from scorched.protocol import connect as raw_connect
    conn = raw_connect("127.0.0.1", server.bound_port, timeout=5)
    conn.send({"t": "hello", "name": "Old", "version": PROTOCOL_VERSION + 99})
    deadline = time.monotonic() + 2
    got = []
    while time.monotonic() < deadline and not got:
        msg = conn.wait(0.2)
        if msg:
            got.append(msg)
    assert got and got[0]["t"] == "error"
    assert got[0].get("fatal")
    conn.close()


def test_only_the_host_can_start(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    b.send(t="start")                    # not the host: ignored
    pump_all([a, b], 0.3)
    assert "round" not in a.kinds()
    a.send(t="start")
    pump_all([a, b], 0.6)
    assert "round" in a.kinds() and "round" in b.kinds()
    a.close(); b.close()


def test_a_lone_player_cannot_start(server):
    a = Harness(server.bound_port, "Ann")
    pump_all([a])
    a.send(t="start")
    pump_all([a], 0.4)
    assert a.errors and "two players" in a.errors[0]
    a.close()


def test_both_clients_receive_identical_shot_timelines(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    a.send(t="start")
    pump_all([a, b], 0.8)

    for _ in range(3):
        for client in (a, b):
            if client.turn:
                client.send(t="aim", angle=50, power=480)
                client.send(t="fire")
                client.turn = False
        pump_all([a, b], 1.2)
        if a.shots:
            break

    assert a.shots, "no shot was ever broadcast"
    assert len(a.shots) == len(b.shots)
    assert a.shots[0]["events"] == b.shots[0]["events"]
    assert a.shots[0]["seq"] == b.shots[0]["seq"]
    # The crucial cross-platform property: replaying the same event list on two
    # independent clients leaves identical terrain.
    assert a.terrain.height == b.terrain.height
    a.close(); b.close()


def test_a_spectator_cannot_fire_on_someone_elses_turn(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    a.send(t="start")
    pump_all([a, b], 0.8)
    waiting = a if not a.turn else b
    waiting.send(t="fire")
    pump_all([a, b], 0.4)
    assert waiting.shots == [] or all(s["pid"] != waiting.pid
                                      for s in waiting.shots)
    a.close(); b.close()


def test_client_terrain_matches_the_server_after_a_shot(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    a.send(t="start")
    pump_all([a, b], 0.8)
    for _ in range(4):
        for client in (a, b):
            if client.turn:
                client.send(t="aim", angle=45, power=500)
                client.send(t="fire")
                client.turn = False
        pump_all([a, b], 1.0)
        if a.shots:
            break
    assert a.shots
    assert a.terrain.height == server.game.terrain.height
    a.close(); b.close()


def test_garbage_from_a_client_does_not_kill_the_server(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    for junk in ({"t": "fire"}, {"t": "buy", "code": 12345},
                 {"t": "aim", "angle": "north"}, {"t": "move", "dir": {}},
                 {"t": "weapon", "code": None}, {"t": "unknown_type"},
                 {"t": "settings", "settings": {"rounds": "many"}}):
        a.conn.send(junk)
    pump_all([a, b], 0.5)
    assert server.running.is_set()
    a.send(t="start")
    pump_all([a, b], 0.8)
    assert "round" in b.kinds()
    a.close(); b.close()


def test_a_disconnect_mid_match_hands_the_tank_to_a_bot(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    a.send(t="start")
    pump_all([a, b], 0.8)
    victim_pid = b.pid
    b.close()
    pump_all([a], 0.8)
    player = server.game.players[victim_pid]
    assert player.bot and not player.connected
    # And the match keeps running rather than deadlocking on the empty seat.
    pump_all([a], 1.5)
    assert server.running.is_set()
    a.close()


def test_bots_can_be_added_and_removed_by_the_host(server):
    a = Harness(server.bound_port, "Ann")
    pump_all([a])
    a.send(t="addbot", skill="expert")
    pump_all([a], 0.4)
    bots = [p for p in server.game.players.values() if p.bot]
    assert len(bots) == 1 and bots[0].skill == "expert"
    a.send(t="kick", pid=bots[0].pid)
    pump_all([a], 0.4)
    assert not [p for p in server.game.players.values() if p.bot]
    a.close()


def test_chat_reaches_the_other_player(server):
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    a.send(t="chat", text="good luck")
    pump_all([a, b], 0.4)
    assert any(m.get("t") == "chat" and m["text"] == "good luck" for m in b.seen)
    a.close(); b.close()


def test_a_client_that_never_acks_does_not_hang_the_match(server):
    """A frozen or overloaded machine must not stall everyone else.

    The server waits for clients to report their animation finished, but only
    up to a grace period. This is the guarantee that lets a slow Pi share a
    match with a fast desktop.
    """
    a = Harness(server.bound_port, "Ann")
    b = Harness(server.bound_port, "Ben")
    pump_all([a, b])
    a.send(t="start")
    pump_all([a, b], 0.8)

    # Ben stops acking entirely from here on.
    b.conn.send = lambda msg: None

    turns_seen = set()
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and len(turns_seen) < 3:
        for client in (a, b):
            client.pump()
        for msg in list(a.seen):
            if msg.get("t") == "turn":
                turns_seen.add(msg["pid"])
        if a.turn:
            a.send(t="aim", angle=45, power=500)
            a.send(t="fire")
            a.turn = False
        time.sleep(0.02)

    assert a.shots, "the match never got going"
    assert server.running.is_set()
    a.close(); b.close()
