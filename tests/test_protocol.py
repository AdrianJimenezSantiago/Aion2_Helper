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


def _packet(hexbody: str) -> bytes:
    body = bytes.fromhex(hexbody)
    return encode_varint(len(body) + 4) + body


def test_login_record_names_your_entity_only_with_the_whole_layout():
    from aion2_helper.protocol import parse_self_identity

    body = b"\x33\x36" + encode_varint(2348) + b"\x01\x02\x03\x04\x07\x06" + "Tester".encode() + b"\x05\x00"
    pkt = encode_varint(len(body) + 4) + body
    o = len(encode_varint(len(body) + 4))
    assert parse_self_identity(pkt, o).entity_id == 2348
    assert parse_self_identity(pkt, o).confirmed
    bare = encode_varint(2348 + 0) + b"\x00" * 3
    pkt = encode_varint(len(bare) + 6) + b"\x33\x36" + bare
    assert not parse_self_identity(pkt, o).confirmed
    events, _ = ServerStreamParser().consume(frame(body))
    assert [(e.entity_id, e.confirmed) for e in events] == [(2348, True)]


def test_live_cast_gives_the_casters_facing():
    from aion2_helper.protocol import EntityFacing, parse_cast_facing

    # Live (2026-10-14): you, ca 03 (458), walking at a move yaw of 119.97 deg.
    walk = _packet("02 38 ca 03 00 c5 8e 15 01 31 00 ca 03 eb ef ef 42 9e 55 01 00")
    fc = parse_cast_facing(walk, 1)
    assert fc.entity_id == 458 and abs(fc.yaw - 120.0) < 0.1
    # A cast at a target, with its point: -7.4 deg, the 352.6 of that player's move yaw.
    aimed = _packet("02 38 a6 21 00 25 d9 a7 00 01 02 a6 21 81 8c ec c0 55 63 ae 42 54 d8 43 42 00 00 00 00 90 4e 01 f4 03")
    fc = parse_cast_facing(aimed, 1)
    assert fc.entity_id == 4262 and abs(fc.yaw - 352.6) < 0.1
    assert parse_cast_facing(walk[:-1], 1) is None  # cut short: not taken
    events, _ = ServerStreamParser().consume(walk)
    assert isinstance(events[0], EntityFacing)


def test_action_pose_gives_position_and_facing():
    from aion2_helper.protocol import EntityMove, parse_action_pose

    body = (
        b"\x3c\x37" + encode_varint(458) + b"\x03" + struct.pack("<fff", 39143.0, 20112.0, 39731.0)
        + struct.pack("<f", 90.0) + b"\x00\x00\x01" + struct.pack("<I", 18190021) + b"\x00" * 4 + b"\x01"
    )  # fmt: skip
    events, _ = ServerStreamParser().consume(frame(body))
    assert events == [EntityMove(458, (39143.0, 20112.0, 39731.0), yaw=90.0)]
    assert parse_action_pose(frame(body[:-1]), 1) is None


def test_spawn_layout_with_a_name_and_a_summons_owner():
    from aion2_helper.protocol import parse_spawn_at

    name = "Kromede".encode()
    named = (
        b"\x41\x36" + encode_varint(900) + bytes([0x0C, 0x00, 0x01, len(name)]) + name
        + struct.pack("<I", 2_100_555) + b"\x00\x02" + struct.pack("<fff", 100.0, 200.0, 300.0) + b"\x00" * 4
    )  # fmt: skip
    sp = parse_spawn_at(named, 0)
    assert (sp.entity_id, sp.code, sp.pos, sp.owner) == (900, 2_100_555, (100.0, 200.0, 300.0), None)
    eid = encode_varint(30632)
    pet = (
        b"\x41\x36" + eid + bytes([0x1F, 0x00, 0x00]) + struct.pack("<I", 3_000_111) + b"\x00\x02"
        + struct.pack("<fff", 1.0e4, 2.0e4, 300.0) + b"\x00" * 6
        + eid + b"\x01\x02" + b"\x00" * 12 + b"\x07\x02\x06" + struct.pack("<I", 7470) + b"\x00"
    )  # fmt: skip
    sp = parse_spawn_at(pet, 0)
    assert sp.code == 3_000_111 and sp.owner == 7470
