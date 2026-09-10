import time

from lanlib.discovery import Beacon, local_addresses, scan


def test_beacon_answers_a_scan():
    info = {"name": "Unit Test", "port": 27099, "players": 3, "max": 8,
            "phase": "lobby"}
    beacon = Beacon(lambda: dict(info), port=27098)
    beacon.start()
    try:
        time.sleep(0.3)
        found = scan(timeout=1.2, port=27098)
    finally:
        beacon.stop()
    names = [entry.get("name") for entry in found]
    assert "Unit Test" in names
    entry = next(e for e in found if e["name"] == "Unit Test")
    assert entry["port"] == 27099
    assert entry["host"]                 # the responder's address is filled in


def test_scan_with_nothing_listening_returns_empty():
    assert scan(timeout=0.4, port=27097) == []


def test_a_second_beacon_on_a_taken_port_fails_softly():
    """Two games on one machine must not crash -- the second just goes quiet."""
    first = Beacon(lambda: {"name": "A", "port": 1}, port=27096)
    second = Beacon(lambda: {"name": "B", "port": 2}, port=27096)
    first.start()
    time.sleep(0.2)
    second.start()
    time.sleep(0.4)
    try:
        assert second.error is not None
    finally:
        first.stop()
        second.stop()


def test_local_addresses_always_returns_something():
    addresses = local_addresses()
    assert addresses and all(isinstance(a, str) for a in addresses)
