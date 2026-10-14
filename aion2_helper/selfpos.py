"""Where the local player is, learned from the client-to-server stream.

The client reports its own movement to the server. Nobody has published that
opcode, so this module finds it: every client packet is scanned for three
floats that look like a world position, keyed by (opcode, offset in packet).
The key that keeps producing positions that move smoothly, and that sit near
the entities the server is spawning around us, is the movement report.

Once learned, the key is remembered in the config (``self_position``) so the
next session uses it straight away.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from .protocol import Vec3, plausible_world_pos, walk

# A position must be at least this far from the world origin on x or y to count:
# it rules out small floats (speeds, angles) that would otherwise look plausible.
MIN_COORD = 100.0
# Fastest believable movement (units per second): mounts and gliding included,
# with room for teleports to be treated as a single bad sample.
MAX_SPEED = 6000.0
# How close (units) a reported position must be to some recently spawned entity.
ANCHOR_RADIUS = 30000.0
LOCK_SAMPLES = 8
LOCK_SPAN_S = 2.0


def distance(a: Vec3, b: Vec3) -> float:
    return math.dist(a, b)


def distance_2d(a: Vec3, b: Vec3) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class _Candidate:
    samples: int = 0
    good: int = 0
    near_anchor: int = 0
    first_t: float = 0.0
    last_t: float = 0.0
    last_pos: Vec3 | None = None


@dataclass
class SelfPositionTracker:
    """Feed it client packets; read ``position`` once ``locked``."""

    fixed_key: tuple[bytes, int] | None = None
    position: Vec3 | None = None
    updated_at: float = 0.0
    _candidates: dict[tuple[bytes, int], _Candidate] = field(default_factory=dict)
    _anchors: list[Vec3] = field(default_factory=list)

    @property
    def locked(self) -> bool:
        return self.fixed_key is not None

    def add_anchor(self, pos: Vec3) -> None:
        """A position the server says an entity is at: we must be somewhere near it."""
        self._anchors.append(pos)
        if len(self._anchors) > 64:
            del self._anchors[:-64]

    def clear_anchors(self) -> None:
        self._anchors.clear()

    def feed(self, stream: bytes, t: float) -> int:
        """Consumes client stream bytes; returns how many were consumed."""
        frames, consumed = walk(stream)
        for f in frames:
            if not f.bundle:
                self._packet(stream[f.start + f.payload_start : f.end], t)
        return consumed

    def _packet(self, body: bytes, t: float) -> None:
        if len(body) < 14:
            return
        op = bytes(body[0:2])
        if self.fixed_key is not None:
            key_op, key_off = self.fixed_key
            if op != key_op or key_off + 12 > len(body):
                return
            x, y, z = struct.unpack_from("<fff", body, key_off)
            if plausible_world_pos(x, y, z):
                self.position = (x, y, z)
                self.updated_at = t
            return

        if len(self._candidates) > 4000:
            self._candidates.clear()  # noise accumulated; start over
        for off in range(2, len(body) - 11):
            x, y, z = struct.unpack_from("<fff", body, off)
            if not plausible_world_pos(x, y, z) or max(abs(x), abs(y)) < MIN_COORD:
                continue
            pos = (x, y, z)
            c = self._candidates.setdefault((op, off), _Candidate(first_t=t))
            if c.last_pos is not None:
                dt = max(t - c.last_t, 0.05)
                if distance(pos, c.last_pos) <= MAX_SPEED * dt + 50:
                    c.good += 1
            c.samples += 1
            if self._anchors and min(distance_2d(pos, a) for a in self._anchors) <= ANCHOR_RADIUS:
                c.near_anchor += 1
            c.last_pos = pos
            c.last_t = t
            self._maybe_lock((op, off), c)

    def _maybe_lock(self, key: tuple[bytes, int], c: _Candidate) -> None:
        if c.samples < LOCK_SAMPLES or c.last_t - c.first_t < LOCK_SPAN_S:
            return
        if c.good < 0.8 * (c.samples - 1):
            return
        # Without anchors we cannot tell a position from a coincidence; wait for spawns.
        if c.near_anchor < 0.8 * c.samples:
            return
        self.fixed_key = key
        self.position = c.last_pos
        self.updated_at = c.last_t
        self._candidates.clear()

    def key_for_config(self) -> dict | None:
        if self.fixed_key is None:
            return None
        op, off = self.fixed_key
        return {"opcode": op.hex(" ").upper(), "offset": off}

    @classmethod
    def from_config(cls, cfg: dict | None) -> "SelfPositionTracker":
        if cfg and cfg.get("opcode") and cfg.get("offset") is not None:
            return cls(fixed_key=(bytes.fromhex(cfg["opcode"]), int(cfg["offset"])))
        return cls()

