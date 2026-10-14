import struct

import lz4.block
import pytest

from aion2_helper.protocol import (
    Despawn,
    ServerStreamParser,
    Spawn,
    ZoneChange,
    encode_varint,
    frame,
    parse_spawn_at,
    read_varint,
    walk,
)

# Two frames from a live capture (2026-10-06), published in A2Tools' tests:
# a `2a 37` record, then a mob spawn (entity 55919, NPC code 2920548).
LIVE = bytes.fromhex(
    "1b2a37e880011d034a065afad1bff9d53a4436337c421740"
    "8f014136efb4031c000064902c0000026b519247339aa5c700540a4600d78b3fc7000107076400000064"
    "000000000000000000000000000000000000006400000064000000010000000000000000000000000000"
    "00000000000601110181969800ffffffffffffffff8075d52abb030000efb40301026b519247339aa5c7"
    "00540a46063ed40000002900000000"
)


def spawn_body(eid: int, code: int, pos=(1000.0, -2000.0, 300.0), kind=0x0C) -> bytes:
    return (
        b"\x41\x36"
        + encode_varint(eid)
        + bytes([kind, 0, 0, 0x64])
        + code.to_bytes(3, "little")
        + b"\x00\x00\x02"
        + struct.pack("<fff", *pos)
        + b"\x00" * 8
    )


def bundle(inner: bytes) -> bytes:
    body = b"\xff\xff" + struct.pack("<I", len(inner)) + lz4.block.compress(inner, store_size=False)
    return frame(body)


def test_varint_roundtrip():
    for v in (0, 1, 127, 128, 300, 55919, 2_000_000):
        enc = encode_varint(v)
        assert read_varint(enc, 0) == (v, len(enc))


def test_walk_splits_frames_and_waits_for_fragments():
    a, b = frame(b"\x23\x36\x01"), frame(b"\x41\x36\x02\x03")
    frames, consumed = walk(a + b + b[:-1])
    assert len(frames) == 2 and consumed == len(a + b)


def test_live_spawn_record():
    events, consumed = ServerStreamParser().consume(LIVE)
    spawns = [e for e in events if isinstance(e, Spawn)]
    assert consumed == len(LIVE)
    assert len(spawns) == 1
    sp = spawns[0]
    assert sp.entity_id == 55919
    assert sp.code == 2_920_548
    assert sp.kind == 0x1C
    assert sp.pos == pytest.approx((74914.84, -84788.4, 8853.0), abs=0.1)


def test_spawn_inside_lz4_bundle():
    inner = frame(spawn_body(60001, 1234567)) + frame(b"\x42\x36" + encode_varint(60001) + b"\x00\x07")
    events, _ = ServerStreamParser().consume(bundle(inner))
    assert isinstance(events[0], Spawn) and events[0].code == 1234567
    assert events[0].pos == pytest.approx((1000.0, -2000.0, 300.0))
    assert events[1] == Despawn(60001, 7)


def test_zone_change_and_death():
    data = frame(b"\x23\x36\x00\x05") + frame(b"\x42\x36" + encode_varint(700) + b"\x00\x03")
    events, _ = ServerStreamParser().consume(data)
    assert isinstance(events[0], ZoneChange)
    assert events[1] == Despawn(700, 3)


def test_spawn_without_position_still_reports_code():
    body = b"\x41\x36" + encode_varint(9000) + bytes([0x0C, 0, 0, 0x64]) + (42).to_bytes(3, "little") + b"\x00\x40\x02"
    sp = parse_spawn_at(body + b"\xff" * 12, 0)
    assert sp is not None and sp.code == 42 and sp.pos is None


def test_garbage_does_not_crash():
    import random

    rnd = random.Random(1)
    parser = ServerStreamParser()
    for _ in range(200):
        parser.consume(bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 400))))
