"""Zero-configuration LAN discovery.

A host announces itself on a UDP port; clients broadcast a probe and collect
replies.  This is the difference between "everyone type 192.168.1.37" and
"click the game in the list", which matters a lot when one of the machines is a
Pi 400 plugged into the TV.

Deliberately tiny and stateless: one datagram out, one datagram back.
"""

from __future__ import annotations

import json
import socket
import threading
import time

from .protocol import DISCOVERY_MAGIC, DISCOVERY_PORT


class Beacon:
    """Answers discovery probes on behalf of a running server."""

    def __init__(self, info_provider, port: int = DISCOVERY_PORT) -> None:
        self.info_provider = info_provider     # callable -> dict
        self.port = port
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, name="discovery",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _serve(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Deliberately no SO_REUSEADDR: on a UDP socket it lets a second
        # process quietly steal the port (and on Windows, steal it from
        # another user), which would make discovery answer at random. One
        # beacon per machine, and the loser says so.
        try:
            sock.bind(("", self.port))
        except OSError as exc:
            # Another game on the same machine already owns the beacon port.
            # Not fatal: players can still join by typing the address.
            self.error = str(exc)
            sock.close()
            return
        sock.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(512)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data.startswith(DISCOVERY_MAGIC):
                    continue
                try:
                    info = self.info_provider()
                    sock.sendto(DISCOVERY_MAGIC + b"|" +
                                json.dumps(info).encode("utf-8"), addr)
                except (OSError, TypeError, ValueError):
                    continue
        finally:
            sock.close()


def scan(timeout: float = 1.2, port: int = DISCOVERY_PORT) -> list[dict]:
    """Broadcast a probe and gather every server that answers in time."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.2)
    found: dict[tuple, dict] = {}
    try:
        for target in _broadcast_targets():
            try:
                sock.sendto(DISCOVERY_MAGIC, (target, port))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data.startswith(DISCOVERY_MAGIC + b"|"):
                continue
            try:
                info = json.loads(data.split(b"|", 1)[1].decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(info, dict):
                continue
            info["host"] = addr[0]
            found[(addr[0], info.get("port"))] = info
    finally:
        sock.close()
    return sorted(found.values(), key=lambda i: (i.get("name", ""), i.get("host", "")))


def _broadcast_targets() -> list[str]:
    """Addresses worth probing.

    The global broadcast address is usually enough, but some Windows setups
    silently drop it, so we also try the /24 broadcast of every local address
    we can see.  Cheap insurance for a two-machine LAN game.
    """
    targets = ["255.255.255.255"]
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if addr.startswith("127."):
                continue
            parts = addr.split(".")
            if len(parts) == 4:
                candidate = ".".join(parts[:3] + ["255"])
                if candidate not in targets:
                    targets.append(candidate)
    except OSError:
        pass
    return targets


def local_addresses() -> list[str]:
    """Best-effort list of this machine's LAN addresses, for the host screen."""
    out: list[str] = []
    # Connecting a UDP socket forces the OS to pick the interface it would
    # actually route from -- no packets are sent.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.1.1", 9))
        out.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127.") and addr not in out:
                out.append(addr)
    except OSError:
        pass
    return out or ["127.0.0.1"]
