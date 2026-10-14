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
    23 36 00 <x f32> <y f32> <z f32> ...     the local player teleported (a zone load): where you are
    23 36 <id varint> <x> <y> <z> ...        any other id: that entity's position update
    21 36 <u32 count> <u32 map id> ...       map load, naming the map from the game's Map table

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


@dataclass
class MapLoad:
    map_id: int


Event = Spawn | Despawn | ZoneChange | EntityMove | MapLoad

# Opcodes whose packets are kept as samples for diagnostics (none carries names).
SAMPLED_OPCODES = {b"\x23\x36", b"\x21\x36", b"\x42\x36", b"\x1d\x37", b"\x1c\x37", b"\x1b\x37", b"\x1a\x37", b"\x00\x8d", b"\x00\x36"}


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


@dataclass
class Opcodes:
    spawn: list[bytes]
    despawn: list[bytes]
    zone_change: list[bytes]
    signatures: list[bytes]
    map_load: list[bytes] = field(default_factory=lambda: [b"\x21\x36"])

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
        )


DEFAULT_OPCODES = Opcodes.from_config({})


class ServerStreamParser:
    """Turns the server-to-client byte stream into entity events."""

    def __init__(self, opcodes: Opcodes = DEFAULT_OPCODES, diag=None):
        self.op = opcodes
        self.diag = diag
        self._spawn_leads = {c[0] for c in opcodes.spawn}

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

    def _bundle(self, payload: bytes, events: list[Event], depth: int = 0) -> None:
        data = decompress_bundle(payload)
        if self.diag is not None:
            self.diag.count("bundles" if data is not None else "bundle_fail")
        if data is None or depth > 4:
            return
        frames, _ = walk(data, inner=True)
        for f in frames:
            if f.bundle:
                self._bundle(data[f.start + f.payload_start : f.end], events, depth + 1)
            else:
                self._packet(data[f.start : f.end], events)

    def _packet(self, packet: bytes, events: list[Event]) -> None:
        _, o = read_varint(packet, 0)
        if o <= 0 or o + 2 > len(packet):
            return
        op = packet[o : o + 2]
        if self.diag is not None:
            self.diag.count("frames")
            self.diag.opcode(op)
            if op in SAMPLED_OPCODES:
                self.diag.sample(op, packet)
        if op in self.op.zone_change:
            eid, l1 = read_varint(packet, o + 2)
            pos = _read_pos(packet, o + 2 + l1) if l1 > 0 else None
            if eid == 0 and l1 > 0:
                events.append(ZoneChange(pos))
                return
            if l1 > 0 and pos is not None and 1 <= eid <= MAX_ENTITY_ID:
                events.append(EntityMove(eid, pos))
                return
        if op in self.op.map_load and o + 10 <= len(packet):
            events.append(MapLoad(struct.unpack_from("<I", packet, o + 6)[0]))
            return
        if op in self.op.despawn:
            eid, l1 = read_varint(packet, o + 2)
            _, l2 = read_varint(packet, o + 2 + max(l1, 0)) if l1 > 0 else (-1, -1)
            flag, l3 = read_varint(packet, o + 2 + l1 + l2) if l2 > 0 else (-1, -1)
            if self.diag is not None and l3 > 0:
                self.diag.count(f"despawn_flag_{flag}")
            if l3 > 0 and flag in (3, 7):
                events.append(Despawn(eid, flag))
                return
        self._scan_spawns(packet, events)

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
