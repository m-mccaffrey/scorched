import json
import struct

import pytest

from lanlib.protocol import Framer, ProtocolError, encode


def test_roundtrip():
    framer = Framer()
    assert framer.feed(encode({"t": "hi", "n": 1})) == [{"t": "hi", "n": 1}]


def test_split_across_reads():
    """A frame arriving in three TCP chunks must decode exactly once."""
    data = encode({"t": "aim", "angle": 47, "power": 501})
    framer = Framer()
    assert framer.feed(data[:2]) == []
    assert framer.feed(data[2:7]) == []
    assert framer.feed(data[7:]) == [{"t": "aim", "angle": 47, "power": 501}]


def test_many_frames_in_one_read():
    blob = b"".join(encode({"t": "x", "i": i}) for i in range(20))
    assert len(Framer().feed(blob)) == 20


def test_oversized_declaration_is_refused():
    framer = Framer()
    with pytest.raises(ProtocolError):
        framer.feed(struct.pack(">I", 1 << 30) + b"junk")


def test_untyped_object_is_refused():
    body = json.dumps({"no_type": True}).encode()
    with pytest.raises(ProtocolError):
        Framer().feed(struct.pack(">I", len(body)) + body)


def test_non_json_body_is_refused():
    body = b"\xff\xfe not json"
    with pytest.raises(ProtocolError):
        Framer().feed(struct.pack(">I", len(body)) + body)
