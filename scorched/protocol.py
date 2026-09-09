"""Wire protocol for Scorched LAN play.

Frames are ``4-byte big-endian length`` + ``UTF-8 JSON body``.  JSON keeps the
protocol debuggable and, more importantly, keeps the client and server free of
any binary-layout assumptions -- the same bytes mean the same thing on an x86
Windows box and on a 32-bit ARM Raspberry Pi.

Every message is a dict with a ``t`` (type) key.  Unknown keys are ignored so
that a slightly newer peer can add fields without breaking an older one.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from queue import Empty, Queue

PROTOCOL_VERSION = 1
DEFAULT_PORT = 27015
DISCOVERY_PORT = 27016
DISCOVERY_MAGIC = b"SCORCHED-DISCOVER-1"

#: Refuse anything larger than this; a terrain sync is ~8 KiB and a busy shot
#: timeline ~64 KiB, so a megabyte is a generous ceiling that still bounds the
#: damage a malformed or hostile peer can do.
MAX_MESSAGE = 1 << 20

_HEADER = struct.Struct(">I")


class ProtocolError(Exception):
    """Raised when a peer sends something we refuse to parse."""


def encode(msg: dict) -> bytes:
    """Serialise one message to a length-prefixed frame."""
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_MESSAGE:
        raise ProtocolError(f"message too large: {len(body)} bytes")
    return _HEADER.pack(len(body)) + body


class Framer:
    """Incremental decoder: feed it socket reads, get whole messages back."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        self._buf.extend(data)
        out = []
        while True:
            if len(self._buf) < _HEADER.size:
                return out
            (size,) = _HEADER.unpack_from(self._buf, 0)
            if size > MAX_MESSAGE:
                raise ProtocolError(f"declared message size {size} too large")
            if len(self._buf) < _HEADER.size + size:
                return out
            start = _HEADER.size
            body = bytes(self._buf[start:start + size])
            del self._buf[:start + size]
            try:
                msg = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProtocolError(f"bad frame: {exc}") from exc
            if not isinstance(msg, dict) or "t" not in msg:
                raise ProtocolError("frame is not a typed object")
            out.append(msg)


def configure(sock: socket.socket) -> None:
    """Apply the socket options every Scorched connection wants.

    ``TCP_NODELAY`` matters here: turn-based traffic is a trickle of small
    messages, and Nagle's algorithm would add up to 200 ms of pure latency to an
    aim update for no bandwidth win at all.
    """
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


class Connection:
    """A socket plus a reader thread that decodes frames into a queue.

    Both ends of the game use this.  Sends are synchronous and serialised by a
    lock; on a LAN the kernel buffer absorbs them without ever blocking a
    caller for a meaningful amount of time.
    """

    def __init__(self, sock: socket.socket, name: str = "peer") -> None:
        configure(sock)
        self.sock = sock
        self.name = name
        self.inbox: "Queue[dict]" = Queue()
        self.closed = threading.Event()
        self.error: str | None = None
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(
            target=self._read_loop, name=f"net-rx-{name}", daemon=True
        )
        self._reader.start()

    # -- reading ---------------------------------------------------------
    def _read_loop(self) -> None:
        framer = Framer()
        try:
            while not self.closed.is_set():
                data = self.sock.recv(65536)
                if not data:
                    break
                for msg in framer.feed(data):
                    self.inbox.put(msg)
        except (OSError, ProtocolError) as exc:
            if not self.closed.is_set():
                self.error = str(exc)
        finally:
            self.closed.set()
            self.inbox.put({"t": "__closed__", "reason": self.error or "disconnected"})

    def poll(self) -> list:
        """Drain every message that has arrived, without blocking."""
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except Empty:
                return out

    def wait(self, timeout: float) -> dict | None:
        try:
            return self.inbox.get(timeout=timeout)
        except Empty:
            return None

    # -- writing ---------------------------------------------------------
    def send(self, msg: dict) -> bool:
        if self.closed.is_set():
            return False
        try:
            frame = encode(msg)
        except ProtocolError:
            return False
        with self._send_lock:
            try:
                self.sock.sendall(frame)
                return True
            except OSError as exc:
                self.error = str(exc)
                self.closed.set()
                return False

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def connect(host: str, port: int, timeout: float = 6.0) -> Connection:
    """Open a client connection, trying every address the name resolves to."""
    last: Exception | None = None
    for family, stype, proto, _canon, addr in socket.getaddrinfo(
        host, port, 0, socket.SOCK_STREAM
    ):
        sock = socket.socket(family, stype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            sock.settimeout(None)
            return Connection(sock, name=f"{host}:{port}")
        except OSError as exc:
            last = exc
            sock.close()
    raise ConnectionError(f"could not connect to {host}:{port}: {last}")
