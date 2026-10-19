"""AION 2 wire protocol: just enough to see entities appear and disappear.

What is known comes from the open-source DPS meters that read the same stream
(A2Tools DPS Meter, domel1998/Aion2DPSMeter, NOIA2...):

    00 ...                                   padding, skipped
    17 03 03 <u16 len> ...                   a TLS record, skipped
    <varint len> <payload>                   a packet; len counts the payload plus 4
    <varint len> FF FF <u32 size> <lz4>      an LZ4 bundle of further packets

    41 36 <id varint> <mask u32> ...         NPC / object / summon spawn (40 36 before June 2026)
          ... <code u24 LE> 00 (00|40) 02 <x f32> <y f32> <z f32> ...
    42 36 <id varint> <0 varint> <flag>      death (flag 3) or gone from the world (flag 7)
    23 36 00 <5 bytes> <x f32> <y f32> <z f32> ...   the local player teleported (a zone load): where you are
    21 36 <u32> <u32 map id> <8 bytes> <x> <y> <z>   map load (Map table id: 1010 = Verteron), and where you are
    1a 37 <id varint> <2 bytes> <x> <y> <z> ...      an entity moved
    1b 37 | 1c 37 <id varint> <flags> [<1 byte> if flags & 1] <x> <y> <z>   an entity moved; your
                                                     own entity too (flags 05 03), a few metres from you
    29 37 <id varint> <2 bytes> <x> <y> <z> <vx> <vy> <vz> ...   a player moving, with its velocity
                                                     (cm/s); yours too, which makes the radar glide
    1f 37 <id varint> <flags> <x> <y> <z> ...        a player's position
    1d 37 <id varint> <flags> [mode] [vx i8] [vy i8] [vz i8] <yaw u16> <yaw u16> [01]
                                                     velocity every 100 ms, in 10 cm/s (flags bits 0-3
                                                     say which fields follow; 01 ends it when bit 0 is
                                                     clear). Integrated between two fixes of yours it
                                                     lands within 35 cm on average (live, 2026-10-14).
    20 37 <id varint> <flags> <5 bytes> <vx f32> <vy f32> <vz f32> ...   velocity in cm/s (flying)
    2a 37 <id varint> <flags> ... <vx f32> <vy f32> <vz f32> <yaw u16>   velocity in cm/s, ten a
                                                     second while flying; matched the 29 37 of the
                                                     same moment (-1149, 967, 550 vs -1162, 961, 536)
    28 37 | 29 37 <id varint> <2 bytes> <x> <y> <z> <vx> <vy> <vz> ...   position and velocity (flying)
    4a 36 <id varint> <n u8> (<stat u16> <value i32>)* ...   your own stats: only ever about you
                                                     (A2Tools), so its id is your entity
    2a 38 | 2b 38 <target> ... <caster varint> <u8> <u32> <u8> <x> <y> <z>   an effect, with its
                                                     caster's position: yours when you cast it on you
    33 36 <id varint> <4 bytes> <07|37|3f> <len u8> <name utf-8> <server u16> ...   the character
                                                     you log in with: your entity, for sure. Only sent
                                                     when you enter the world from character selection
                                                     (PetTracker, 0Fox2Give), so start before logging in
    02 38 <id varint> 00 <skill u32> <u8> <00|02> <target varint> <facing f32 deg> [<x> <y> <z> if 02]
          <3 varints>                                a skill cast, with where its caster faces (same
                                                     angle as the move yaw: 120.0 vs 119.97, live);
                                                     your flight buffs send it about once a second
    3c 37 <id varint> 03 <x> <y> <z> <facing f32 deg> (00 00|07 02) 01 <skill u32> <4 bytes> <varint>
                                                     an entity acting: its position and facing (PetTracker)
    34 36 <id varint> <kind u8> 00 <code u32> <x> <y> <z> ...   an object appeared: gathering
                                                     nodes (1500000 sat on an odyle node to 0.0 m),
                                                     Empyrean Traces, and the Hidden Cubes
    45 36 <id varint> ...                            another player appeared (carries names: never sampled)

Layouts of 23 36, 21 36 and 1a-1c 37 were read off a live capture of 2026-10-14
(Verteron), where the positions all agree to a few metres.

The client's own packets are encrypted (A2Tools, ping_tracker.rs), so where you
are can only come from the server: the teleport record, and updates of your own
entity once its id is known.

The position triplet after the NPC code is not used by the DPS meters; it was
identified from their published capture samples (Unreal units, centimetres).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

try:
    import lz4.block as _lz4
except ImportError:  # pragma: no cover - only hit when the dependency is missing
    _lz4 = None

MAX_PACKET_BYTES = 65535
MAX_FRAGMENT_WAIT_BYTES = 16384
# A bundle may run past that: the zone-load bundle, which carries the spawns
# around you, was 17 KB in a capture of 2026-10-09 (A2Tools). Its FF FF header
# is what tells it from a garbage length, so bundles are waited for up to this.
MAX_BUNDLE_WAIT_BYTES = MAX_PACKET_BYTES
MAX_DECOMPRESSED_BYTES = 1_000_000
MAX_ENTITY_ID = 9_999_999

Vec3 = tuple[float, float, float]


# ───── varints ─────


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Little-endian base-128 varint at ``offset``. Returns (value, length); length -1 if invalid."""
    value = 0
    shift = 0
    count = 0
    n = len(data)
    while True:
        if offset < 0 or offset + count >= n:
            return -1, -1
        b = data[offset + count]
        count += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, count
        shift += 7
        if shift >= 32:
            return -1, -1


def encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value == 0:
            out.append(b)
            return bytes(out)
        out.append(b | 0x80)


def frame(body: bytes) -> bytes:
    """Wraps a packet body the way the server does (used by tests and the demo)."""
    return encode_varint(len(body) + 4) + body


# ───── framing ─────


@dataclass(frozen=True)
class Frame:
    start: int
    end: int
    payload_start: int  # relative to start
    bundle: bool


def _frame_size(value: int, varint_len: int) -> int | None:
    size = value - 4 + varint_len
    return size if size >= varint_len and size > 0 else None


def _tls_record_size(data: bytes, at: int) -> int | None:
    if len(data) - at < 5:
        return None
    t, major, minor = data[at], data[at + 1], data[at + 2]
    if not (0x14 <= t <= 0x17 and major == 0x03 and 0x01 <= minor <= 0x04):
        return None
    ln = (data[at + 3] << 8) | data[at + 4]
    return 5 + ln if 0 < ln <= 16384 + 256 else None


def _looks_like_bundle(data: bytes, at: int) -> bool | None:
    """Whether the frame payload at ``at`` starts like a bundle; None if too few bytes yet."""
    if at + 6 > len(data):
        return None
    if data[at] != 0xFF or data[at + 1] != 0xFF:
        return False
    size = struct.unpack_from("<I", data, at + 2)[0]
    return 0 < size <= MAX_DECOMPRESSED_BYTES


def walk(data: bytes, inner: bool = False, stats: dict | None = None) -> tuple[list[Frame], int]:
    """Splits a stream buffer into frames, stopping at the first incomplete one.

    Returns the frames and how many bytes were consumed. A capture that starts
    mid-stream is normal, so invalid lengths resynchronise a byte at a time.
    ``inner`` walks a decompressed bundle, which the game framed itself.
    """
    frames: list[Frame] = []
    offset = 0
    n = len(data)
    resync = 0
    while offset < n:
        if data[offset] == 0x00:
            offset += 1
            continue
        if not inner:
            tls = _tls_record_size(data, offset)
            if tls is not None:
                if offset + tls > n:
                    break
                offset += tls
                continue
        value, vlen = read_varint(data, offset)
        if vlen <= 0 or value <= 0:
            if inner or offset + 5 > n:
                break
            offset += 1
            resync += 1
            continue
        total = _frame_size(value, vlen)
        if total is None or total > MAX_PACKET_BYTES:
            offset += 1
            resync += 1
            continue
        if offset + total > n:
            if inner:
                break
            if total > MAX_FRAGMENT_WAIT_BYTES:
                bundle = _looks_like_bundle(data, offset + vlen)
                if bundle is False or total > MAX_BUNDLE_WAIT_BYTES:
                    offset += 1
                    resync += 1
                    continue
            break  # a legitimate fragment: wait for more bytes
        is_bundle = vlen + 1 < total and data[offset + vlen] == 0xFF and data[offset + vlen + 1] == 0xFF
        frames.append(Frame(offset, offset + total, vlen, is_bundle))
        offset += total
    if stats is not None and resync:
        stats["resync"] = stats.get("resync", 0) + resync
    return frames, offset


def decompress_bundle(payload: bytes) -> bytes | None:
    """``FF FF <u32 LE size> <lz4 block>`` → the decompressed packets."""
    if _lz4 is None or len(payload) < 7:
        return None
    size = struct.unpack_from("<I", payload, 2)[0]
    if size <= 0 or size > MAX_DECOMPRESSED_BYTES:
        return None
    try:
        out = _lz4.decompress(payload[6:], uncompressed_size=size)
    except Exception:
        return None
    return out if len(out) == size else None


# ───── events ─────


@dataclass
class Spawn:
    entity_id: int
    kind: int
    code: int | None
    pos: Vec3 | None
    raw: bytes = field(repr=False, default=b"")


@dataclass
class Despawn:
    entity_id: int
    flag: int


@dataclass
class ZoneChange:
    pos: Vec3 | None = None  # where the local player landed


@dataclass
class EntityMove:
    entity_id: int
    pos: Vec3
    velocity: Vec3 | None = None  # units (cm) per second, when the record carries it
    flags: int | None = None  # the byte after the id in 1b 37 / 1c 37: 05 for your own entity (live)
    yaw: float | None = None  # facing, degrees, Unreal convention (atan2(dy, dx)); u16 after the position
    phase: str | None = None  # "start" (1b 37), "move" (1c 37) or "stop" (1a 37)
    learned: bool = False  # from an opcode learned this session: to be checked against known positions


@dataclass
class MapLoad:
    map_id: int
    pos: Vec3 | None = None  # where the local player is


@dataclass
class EntityVelocity:
    entity_id: int
    vx: float  # cm/s
    vy: float
    vz: float | None = None
    yaw: float | None = None


@dataclass
class SelfIdentity:
    entity_id: int
    confirmed: bool = False  # the whole login layout (with a name) was there, not only the opcode


@dataclass
class EntityFacing:
    """Where an entity faces, from a skill cast: degrees, the move yaw's convention (atan2(dy, dx))."""

    entity_id: int
    yaw: float
    target: int | None = None
    target_pos: Vec3 | None = None  # the target's world point, when the cast carries it (mode 02)


@dataclass
class SelfStats:
    """A stats record: only ever sent about your own character."""

    entity_id: int


@dataclass
class PlayerSpawn:
    entity_id: int


Event = (
    Spawn | Despawn | ZoneChange | EntityMove | MapLoad | SelfIdentity | PlayerSpawn | EntityVelocity | SelfStats
    | EntityFacing
)

SELF_STATS_OPCODE = b"\x4a\x36"
TAIL_VELOCITY_OPCODE = b"\x2a\x37"  # velocity floats + yaw at the end

VELOCITY_OPCODE = b"\x1d\x37"
VELOCITY_UNIT = 10.0  # cm/s per unit of the 1d 37 bytes (fitted on a live walk: exactly 10)
FLOAT_VELOCITY_OPCODE = b"\x20\x37"
EFFECT_OPCODES = (b"\x2a\x38", b"\x2b\x38")
CAST_OPCODE = b"\x02\x38"
ACTION_POSE_OPCODE = b"\x3c\x37"
SKILL_MIN, SKILL_MAX = 11_000_000, 20_000_000
LOGIN_NAME_MARKS = (0x07, 0x37, 0x3F)


def _i8(b: int) -> int:
    return b - 256 if b > 127 else b


def parse_velocity(packet: bytes, o: int) -> "EntityVelocity | None":
    """``1d 37``: the compact velocity record, sent ten times a second per moving entity."""
    eid, l1 = read_varint(packet, o + 2)
    if l1 <= 0 or not (1 <= eid <= MAX_ENTITY_ID):
        return None
    at = o + 2 + l1
    if at >= len(packet):
        return None
    flags = packet[at]
    i = at + 1
    fields: dict[int, int] = {}
    for bit in range(4):
        if flags & (1 << bit):
            if i >= len(packet):
                return None
            fields[bit] = packet[i]
            i += 1
    yaw = struct.unpack_from("<H", packet, i)[0] * 360.0 / 65536 if i + 2 <= len(packet) else None
    vx = _i8(fields[1]) * VELOCITY_UNIT if 1 in fields else 0.0
    vy = _i8(fields[2]) * VELOCITY_UNIT if 2 in fields else 0.0
    vz = _i8(fields[3]) * VELOCITY_UNIT if 3 in fields else None
    return EntityVelocity(eid, vx, vy, vz, yaw)


def parse_float_velocity(packet: bytes, o: int) -> "EntityVelocity | None":
    """``20 37``: a velocity in floats (cm/s), seen while flying."""
    eid, l1 = read_varint(packet, o + 2)
    at = o + 2 + l1 + 6
    if l1 <= 0 or not (1 <= eid <= MAX_ENTITY_ID) or at + 12 > len(packet):
        return None
    v = struct.unpack_from("<fff", packet, at)
    if not all(math.isfinite(c) and abs(c) < 10000 for c in v):
        return None
    return EntityVelocity(eid, v[0], v[1], v[2])


def parse_tail_velocity(packet: bytes, o: int) -> "EntityVelocity | None":
    """``2a 37``: ... <vx> <vy> <vz> <yaw u16> at the end of the record."""
    eid, l1 = read_varint(packet, o + 2)
    n = len(packet)
    if l1 <= 0 or not (1 <= eid <= MAX_ENTITY_ID) or n - 14 < o + 2 + l1 + 1:
        return None
    v = struct.unpack_from("<fff", packet, n - 14)
    if not all(math.isfinite(c) and abs(c) < 10000 for c in v):
        return None
    yaw = struct.unpack_from("<H", packet, n - 2)[0] * 360.0 / 65536
    return EntityVelocity(eid, v[0], v[1], v[2], yaw)


def parse_effect_caster_pos(packet: bytes, o: int) -> "EntityMove | None":
    """``2a/2b 38``: an effect on <target>, ending with its caster and the caster's position.

    Only kept when target and caster are the same entity (an effect you put on yourself, as a
    mount or a flight buff does every second or so): then it is that entity's position."""
    target, l1 = read_varint(packet, o + 2)
    n = len(packet)
    if l1 <= 0 or n < o + 2 + l1 + 24:
        return None
    pos = _read_pos(packet, n - 12)
    if pos is None:
        return None
    end = n - 12 - 1 - 4 - 1  # <caster> <u8> <u32> <u8> <x> <y> <z>
    for k in (1, 2, 3):
        caster, ln = read_varint(packet, end - k)
        if ln == k and caster == target:
            return EntityMove(target, pos)
    return None

def parse_self_identity(packet: bytes, o: int) -> SelfIdentity:
    """``33 36``: confirmed only when the login layout is all there; the name is checked, never kept."""
    eid, l1 = read_varint(packet, o + 2)
    p = o + 2 + l1
    if l1 <= 0 or not (100 <= eid <= 999_999) or p + 6 > len(packet):
        return SelfIdentity(eid if l1 > 0 else 0)
    p += 4
    n = packet[p + 1]
    if packet[p] not in LOGIN_NAME_MARKS or not (1 <= n <= 30) or p + 2 + n + 2 > len(packet):
        return SelfIdentity(eid)
    try:
        name = packet[p + 2 : p + 2 + n].decode("utf-8")
    except UnicodeDecodeError:
        return SelfIdentity(eid)
    ok = any(c.isalpha() for c in name) and all(c.isalnum() or c in "'-_" for c in name)
    return SelfIdentity(eid, ok)


def _float_deg(packet: bytes, at: int) -> float | None:
    if at + 4 > len(packet):
        return None
    d = struct.unpack_from("<f", packet, at)[0]
    return d if math.isfinite(d) and -180.0 <= d <= 360.0 else None


def parse_cast_facing(packet: bytes, o: int) -> EntityFacing | None:
    """``02 38``: a skill cast and where its caster faces. Every field is checked to the end."""
    eid, l1 = read_varint(packet, o + 2)
    p = o + 2 + l1
    if l1 <= 0 or eid <= 0 or p + 7 > len(packet) or packet[p] != 0:
        return None
    skill = struct.unpack_from("<I", packet, p + 1)[0]
    mode = packet[p + 6]
    if not (SKILL_MIN <= skill < SKILL_MAX) or mode not in (0, 2):
        return None
    target, l2 = read_varint(packet, p + 7)
    if l2 <= 0 or target <= 0:
        return None
    p += 7 + l2
    deg = _float_deg(packet, p)
    if deg is None:
        return None
    p += 4
    target_pos = None
    if mode == 2:  # the target's point: a world position when aimed at another (87, 49, 0 at yourself, live)
        if p + 12 > len(packet) or not all(math.isfinite(v) for v in struct.unpack_from("<fff", packet, p)):
            return None
        target_pos = _read_pos(packet, p) if target != eid else None
        p += 12
    for _ in range(3):
        _, ln = read_varint(packet, p)
        if ln <= 0:
            return None
        p += ln
    return EntityFacing(eid, deg % 360, target, target_pos) if p == len(packet) else None


def parse_action_pose(packet: bytes, o: int) -> EntityMove | None:
    """``3c 37``: an entity acting, with its position and facing (layout from PetTracker)."""
    eid, l1 = read_varint(packet, o + 2)
    p = o + 2 + l1
    if l1 <= 0 or not (1 <= eid <= MAX_ENTITY_ID) or p + 29 > len(packet) or packet[p] != 0x03:
        return None
    pos = _read_pos(packet, p + 1)
    deg = _float_deg(packet, p + 13)
    if pos is None or deg is None:
        return None
    p += 17
    if packet[p : p + 2] not in (b"\x00\x00", b"\x07\x02") or packet[p + 2] != 0x01:
        return None
    skill = struct.unpack_from("<I", packet, p + 3)[0]
    _, ln = read_varint(packet, p + 11)
    if not (SKILL_MIN <= skill < SKILL_MAX) or ln <= 0 or p + 11 + ln != len(packet):
        return None
    return EntityMove(eid, pos, yaw=deg % 360)


# Movement records not in the table are learned: an opcode whose short packets keep carrying
# <id varint> <skip bytes> <plausible position> is taken as one, with that skip.
LEARN_MOVE_MAX_LEN = 80
LEARN_MOVE_MIN_PACKETS = 12
LEARN_MOVE_SHARE = 0.85


def _move_pos(packet: bytes, at: int, skip: int) -> Vec3 | None:
    found = _move_pos_at(packet, at, skip)
    return found[0] if found else None


def _velocity(packet: bytes, at: int) -> Vec3 | None:
    """Three floats right after a position, if they look like a speed (under 100 m/s)."""
    if at + 12 > len(packet):
        return None
    v = struct.unpack_from("<fff", packet, at)
    if all(math.isfinite(c) and abs(c) < 10000 for c in v):
        return v
    return None


def _move_pos_at(packet: bytes, at: int, skip: int) -> tuple[Vec3, int] | None:
    """A movement record's position: at ``skip`` bytes past the id, or where its flags byte says
    (bit 0 set: one more byte before the position), or a byte either side."""
    tries = [skip]
    if at < len(packet):
        tries.append(2 if packet[at] & 1 else 1)
    tries += [1, 2, 3, 0]
    seen = set()
    for k in tries:
        if k in seen:
            continue
        seen.add(k)
        pos = _read_pos(packet, at + k)
        if pos is not None and max(abs(pos[0]), abs(pos[1])) >= 100:
            return pos, at + k
    return None


# 1b 37 starts a walk, 1c 37 reports it every ~2 m, 1a 37 ends it (a shorter last step), live.
MOVE_PHASES = {b"\x1b\x37": "start", b"\x1c\x37": "move", b"\x1a\x37": "stop"}

# Bytes between the entity id and the position in a 23 36 teleport / position record.
TELEPORT_POS_SKIP = 5
# Offset of the position after the 21 36 opcode.
MAP_LOAD_POS_AT = 16

# Records that carry character names: never traced or sampled.
NAMED_OPCODES = {b"\x33\x36", b"\x45\x36", b"\x44\x36"}

# Opcodes whose packets are kept as samples for diagnostics (none carries names).
SAMPLED_OPCODES = {b"\x23\x36", b"\x21\x36", b"\x42\x36", b"\x1d\x37", b"\x1c\x37", b"\x1b\x37", b"\x1a\x37",
                   b"\x29\x37", b"\x2a\x37", b"\x1f\x37", b"\x4b\x36", b"\x34\x36"}


def plausible_world_pos(x: float, y: float, z: float) -> bool:
    """Whether three floats look like a world position (Unreal units)."""
    if not all(math.isfinite(v) for v in (x, y, z)):
        return False
    if abs(x) > 5e6 or abs(y) > 5e6 or abs(z) > 1e6:
        return False
    # Real coordinates are never all tiny; random bytes decoding to denormals are.
    return abs(x) >= 1.0 or abs(y) >= 1.0


def _read_pos(data: bytes, at: int) -> Vec3 | None:
    if at + 12 > len(data):
        return None
    x, y, z = struct.unpack_from("<fff", data, at)
    return (x, y, z) if plausible_world_pos(x, y, z) else None


def parse_spawn_at(data: bytes, at: int) -> Spawn | None:
    """Parses a spawn record whose opcode starts at ``at``."""
    eid, elen = read_varint(data, at + 2)
    if elen <= 0 or not (1 <= eid <= MAX_ENTITY_ID):
        return None
    off = at + 2 + elen
    if off + 4 > len(data):
        return None
    kind = data[off]
    code = None
    pos = None
    end = min(len(data) - 2, off + 60)
    for s in range(off + 3, end):
        if data[s] == 0x00 and data[s + 1] in (0x00, 0x40) and data[s + 2] == 0x02:
            code = data[s - 3] | data[s - 2] << 8 | data[s - 1] << 16
            pos = _read_pos(data, s + 3)
            break
    if code is None:
        return None  # not a record we understand (or a false opcode hit)
    return Spawn(eid, kind, code, pos, bytes(data[at : min(len(data), at + 96)]))


def parse_object_spawn(packet: bytes, o: int) -> Spawn | None:
    """``34 36 <id> <kind> 00 <code u32> <x> <y> <z>``: an object (node, trace, cube) appeared."""
    eid, l1 = read_varint(packet, o + 2)
    if l1 <= 0 or not (1 <= eid <= MAX_ENTITY_ID):
        return None
    at = o + 2 + l1
    if at + 18 > len(packet):
        return None
    code = struct.unpack_from("<I", packet, at + 2)[0]
    pos = _read_pos(packet, at + 6)
    if not (1 <= code <= MAX_ENTITY_ID) or pos is None:
        return None
    return Spawn(eid, packet[at], code, pos, bytes(packet[o : min(len(packet), o + 96)]))


@dataclass
class Opcodes:
    spawn: list[bytes]
    despawn: list[bytes]
    zone_change: list[bytes]
    signatures: list[bytes]
    map_load: list[bytes] = field(default_factory=lambda: [b"\x21\x36"])
    # Movement records: opcode -> bytes between the entity id and the position.
    moves: dict[bytes, int] = field(
        default_factory=lambda: {
            b"\x1a\x37": 2, b"\x1b\x37": 1, b"\x1c\x37": 1, b"\x1f\x37": 1, b"\x28\x37": 2, b"\x29\x37": 2
        }  # fmt: skip
    )
    # Movement records followed by a velocity.
    velocity_moves: list[bytes] = field(default_factory=lambda: [b"\x28\x37", b"\x29\x37"])
    self_identity: list[bytes] = field(default_factory=lambda: [b"\x33\x36"])
    object_spawn: list[bytes] = field(default_factory=lambda: [b"\x34\x36"])
    player_spawn: list[bytes] = field(default_factory=lambda: [b"\x45\x36", b"\x44\x36"])

    @staticmethod
    def _parse(codes: list[str]) -> list[bytes]:
        return [bytes.fromhex(c) for c in codes if c.strip()]

    @classmethod
    def from_config(cls, cfg: dict) -> "Opcodes":
        return cls(
            spawn=cls._parse(cfg.get("spawn", ["41 36"])),
            despawn=cls._parse(cfg.get("despawn", ["42 36"])),
            zone_change=cls._parse(cfg.get("zone_change", ["23 36"])),
            signatures=cls._parse(cfg.get("signatures", ["0E 00 36", "06 00 36"])),
            map_load=cls._parse(cfg.get("map_load", ["21 36"])),
            moves={
                bytes.fromhex(k): int(v)
                for k, v in cfg.get("moves", {"1A 37": 2, "1B 37": 1, "1C 37": 1, "1F 37": 1, "28 37": 2, "29 37": 2}).items()
            },
            velocity_moves=cls._parse(cfg.get("velocity_moves", ["28 37", "29 37"])),
            self_identity=cls._parse(cfg.get("self_identity", ["33 36"])),
            object_spawn=cls._parse(cfg.get("object_spawn", ["34 36"])),
            player_spawn=cls._parse(cfg.get("player_spawn", ["45 36", "44 36"])),
        )


DEFAULT_OPCODES = Opcodes.from_config({})


class ServerStreamParser:
    """Turns the server-to-client byte stream into entity events."""

    def __init__(self, opcodes: Opcodes = DEFAULT_OPCODES, diag=None):
        import dataclasses

        self.op = dataclasses.replace(opcodes, moves=dict(opcodes.moves))  # learned moves stay with this parser
        self._learned_moves: set[bytes] = set()
        self.diag = diag
        self.watch_id: int | None = None  # your entity: every packet about it goes to the self trace
        self._spawn_leads = {c[0] for c in opcodes.spawn}
        # opcode -> [packets seen, hits per skip 0..3]
        self._move_stats: dict[bytes, list[int]] = {}
        self._not_moves = {CAST_OPCODE, TAIL_VELOCITY_OPCODE, VELOCITY_OPCODE, FLOAT_VELOCITY_OPCODE, SELF_STATS_OPCODE} | (  # 2a 37: seen live, not a position
            set(opcodes.spawn) | set(opcodes.despawn) | set(opcodes.zone_change) | set(opcodes.map_load)
            | set(opcodes.self_identity) | set(opcodes.object_spawn) | set(opcodes.player_spawn)
        )  # fmt: skip

    def consume(self, buffer: bytes) -> tuple[list[Event], int]:
        events: list[Event] = []
        stats: dict = {}
        frames, consumed = walk(buffer, stats=stats)
        if self.diag is not None and stats.get("resync"):
            self.diag.count("resync", stats["resync"])
        for f in frames:
            if f.bundle:
                self._bundle(buffer[f.start + f.payload_start : f.end], events)
            else:
                self._packet(buffer[f.start : f.end], events)
        return events, consumed

    def _bundle(self, payload: bytes, events: list[Event], depth: int = 0, embedded: bool = False) -> None:
        if depth > 4:
            return
        data = decompress_bundle(payload)
        if self.diag is not None:
            if embedded:
                if data is not None:
                    self.diag.count("embedded_bundles")
            else:
                self.diag.count("bundles" if data is not None else "bundle_fail")
        if data is None or depth > 4:
            return
        frames, _ = walk(data, inner=True)
        for f in frames:
            if f.bundle:
                self._bundle(data[f.start + f.payload_start : f.end], events, depth + 1)
            else:
                self._packet(data[f.start : f.end], events, depth + 1)

    def _packet(self, packet: bytes, events: list[Event], depth: int = 0) -> None:
        _, o = read_varint(packet, 0)
        if o <= 0 or o + 2 > len(packet):
            return
        op = packet[o : o + 2]
        if self.diag is not None:
            self.diag.count("frames")
            self.diag.opcode(op)
            if op in SAMPLED_OPCODES:
                self.diag.sample(op, packet)
            if op not in NAMED_OPCODES:
                if self.watch_id is not None:
                    eid, l1 = read_varint(packet, o + 2)
                    if l1 > 0 and eid == self.watch_id:
                        self.diag.self_packet(op, packet)
                elif op[1] == 0x37 or op in self.op.zone_change or op in self.op.map_load or op == SELF_STATS_OPCODE:
                    self.diag.trace_packet(op, packet)  # you are among these: kept to find out which
        if op in self.op.zone_change:
            eid, l1 = read_varint(packet, o + 2)
            if l1 > 0:
                pos = _read_pos(packet, o + 2 + l1 + TELEPORT_POS_SKIP) or _read_pos(packet, o + 2 + l1)
                if eid == 0:
                    events.append(ZoneChange(pos))
                    return
                if pos is not None and 1 <= eid <= MAX_ENTITY_ID:
                    events.append(EntityMove(eid, pos))
                    return
        if op == VELOCITY_OPCODE:
            v = parse_velocity(packet, o)
            if v is not None:
                events.append(v)
            return
        if op == FLOAT_VELOCITY_OPCODE:
            v = parse_float_velocity(packet, o)
            if v is not None:
                events.append(v)
                return
        if op == TAIL_VELOCITY_OPCODE:
            v = parse_tail_velocity(packet, o)
            if v is not None:
                events.append(v)
            return
        if op == SELF_STATS_OPCODE:
            eid, l1 = read_varint(packet, o + 2)
            if l1 > 0 and 1 <= eid <= MAX_ENTITY_ID:
                events.append(SelfStats(eid))
            return
        if op == CAST_OPCODE:
            fc = parse_cast_facing(packet, o)
            if fc is not None:
                events.append(fc)
            return
        if op == ACTION_POSE_OPCODE:
            mv = parse_action_pose(packet, o)
            if mv is not None:
                events.append(mv)
                return
        if op in EFFECT_OPCODES:
            mv = parse_effect_caster_pos(packet, o)
            if mv is not None:
                events.append(mv)
            return
        if op in self.op.moves:
            eid, l1 = read_varint(packet, o + 2)
            found = _move_pos_at(packet, o + 2 + l1, self.op.moves[op]) if l1 > 0 else None
            if found is not None and 1 <= eid <= MAX_ENTITY_ID:
                pos, at = found
                vel = _velocity(packet, at + 12) if op in self.op.velocity_moves else None
                flags = packet[o + 2 + l1] if op in (b"\x1b\x37", b"\x1c\x37") and o + 2 + l1 < len(packet) else None
                yaw = phase = None
                if op in MOVE_PHASES and at + 14 <= len(packet):
                    # A monster stepping 1.99 m every 1c 37 moved exactly along this yaw (206.0 deg, live).
                    yaw = struct.unpack_from("<H", packet, at + 12)[0] * 360.0 / 65536
                    phase = MOVE_PHASES[op]
                events.append(EntityMove(eid, pos, vel, flags, yaw, phase, op in self._learned_moves))
            return
        if len(packet) <= LEARN_MOVE_MAX_LEN and op[1] in (0x36, 0x37, 0x38) and self._learn_move(op, packet, o):
            return
        if op in self.op.map_load and o + 10 <= len(packet):
            events.append(MapLoad(struct.unpack_from("<I", packet, o + 6)[0], _read_pos(packet, o + 2 + MAP_LOAD_POS_AT)))
            return
        if op in self.op.self_identity:
            si = parse_self_identity(packet, o)
            if 1 <= si.entity_id <= MAX_ENTITY_ID:
                events.append(si)
        if op in self.op.player_spawn:
            eid, l1 = read_varint(packet, o + 2)
            if l1 > 0 and 1 <= eid <= MAX_ENTITY_ID:
                events.append(PlayerSpawn(eid))
        if op in self.op.object_spawn:
            sp = parse_object_spawn(packet, o)
            if sp is not None:
                events.append(sp)
                return
        if op in self.op.despawn:
            eid, l1 = read_varint(packet, o + 2)
            _, l2 = read_varint(packet, o + 2 + max(l1, 0)) if l1 > 0 else (-1, -1)
            flag, l3 = read_varint(packet, o + 2 + l1 + l2) if l2 > 0 else (-1, -1)
            if self.diag is not None and l3 > 0:
                self.diag.count(f"despawn_flag_{flag}")
            # 3 death, 7 gone; 1 is sent when an entity leaves your view (seen live 2026-10-14).
            if l3 > 0 and flag in (1, 3, 7):
                events.append(Despawn(eid, flag))
                return
        self._scan_spawns(packet, events)
        if depth < 3:
            self._embedded_bundles(packet, events, depth)

    def _learn_move(self, op: bytes, packet: bytes, o: int) -> bool:
        """Counts how often ``op`` looks like <id> <skip> <position>; adopts it once it clearly does."""
        if op in self._not_moves:
            return False
        eid, l1 = read_varint(packet, o + 2)
        if l1 <= 0 or not (1 <= eid <= MAX_ENTITY_ID):
            return False
        st = self._move_stats.setdefault(bytes(op), [0, 0, 0, 0, 0])
        st[0] += 1
        for k in range(4):
            pos = _read_pos(packet, o + 2 + l1 + k)
            if pos is not None and max(abs(pos[0]), abs(pos[1])) >= 100:
                st[1 + k] += 1
        if st[0] >= LEARN_MOVE_MIN_PACKETS:
            best = max(range(4), key=lambda k: st[1 + k])
            if st[1 + best] >= LEARN_MOVE_SHARE * st[0]:
                self.op.moves[bytes(op)] = best
                self._learned_moves.add(bytes(op))
                if self.diag is not None:
                    self.diag.learned_moves[op.hex(" ")] = best
                from .diag import log

                log.info("Opcode de movimiento aprendido: %s (salto %s)", op.hex(" "), best)
                return False
            if st[0] > 200:
                self._not_moves.add(bytes(op))
        return False

    def _embedded_bundles(self, packet: bytes, events: list[Event], depth: int) -> None:
        """LZ4 bundles carried inside a larger packet, where the framing never opens them.

        A2Tools found the self record in them; spawns ride there too (a live capture
        had 26 removals for 2 spawns read). ``<varint len> FF FF <size u32> <lz4>``."""
        i = 2
        n = len(packet)
        while i + 8 < n:
            if packet[i] != 0xFF or packet[i + 1] != 0xFF:
                i += 1
                continue
            end = None
            for k in (1, 2, 3):
                at = i - k
                if at < 1:
                    continue
                value, vlen = read_varint(packet, at)
                if vlen != k or value <= 0:
                    continue
                total = _frame_size(value, vlen)
                if total is not None and total > vlen + 6 and at + total <= n:
                    end = at + total
                    break
            if end is None:
                i += 1
                continue
            self._bundle(packet[i:end], events, depth + 1, embedded=True)
            i = end

    def _scan_spawns(self, data: bytes, events: list[Event]) -> None:
        """Spawn records sit at the front of a packet or embedded in a larger one."""
        i = 0
        n = len(data)
        while i + 6 < n:
            if data[i] not in self._spawn_leads or data[i : i + 2] not in self.op.spawn:
                i += 1
                continue
            if i > 0 and data[i - 1] == 0x00:
                i += 2
                continue
            sp = parse_spawn_at(data, i)
            if sp is not None:
                events.append(sp)
            i += 2
