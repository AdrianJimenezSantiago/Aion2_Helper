"""World state: which entities are around us, and which of them are Hidden Cubes."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from .config import Config
from .protocol import Despawn, Event, Spawn, Vec3, ZoneChange
from .selfpos import SelfPositionTracker, distance, distance_2d
from .spots import SpotStore

MAX_ENTITIES = 5000
# Tagging picks the entity nearest to you, but only within this many metres.
TAG_RADIUS_M = 8.0


@dataclass
class Entity:
    entity_id: int
    code: int | None
    kind: int
    pos: Vec3 | None
    first_seen: float
    last_seen: float


@dataclass
class EntityView:
    entity: Entity
    is_cube: bool
    distance_m: float | None
    bearing_deg: float | None  # 0 = +X ("north"), clockwise
    age_s: float


@dataclass
class Snapshot:
    status: str
    self_pos: Vec3 | None
    self_learned: bool
    cubes: list[EntityView]
    nearby: list[EntityView]
    spots: list[Vec3]
    entity_count: int


def bearing(src: Vec3, dst: Vec3) -> float:
    """Compass bearing from src to dst, with +X as north and +Y as east."""
    return math.degrees(math.atan2(dst[1] - src[1], dst[0] - src[0])) % 360


class CubeDetector:
    def __init__(
        self,
        config: Config,
        tracker: SelfPositionTracker | None = None,
        on_new_cube: Callable[[Entity], None] | None = None,
        discovery_log: Path | None = None,
        spots: SpotStore | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.tracker = tracker or SelfPositionTracker.from_config(config["self_position"])
        self.on_new_cube = on_new_cube
        self.clock = clock
        self.spots = spots or SpotStore(None)
        self.status = "Arrancando"
        self._entities: dict[int, Entity] = {}
        self._lock = threading.RLock()
        self._log: TextIO | None = open(discovery_log, "a", encoding="utf-8") if discovery_log else None
        self._saved_selfpos = config["self_position"] is not None

    # ───── codes ─────

    @property
    def cube_codes(self) -> set[int]:
        return set(self.config["cube_codes"])

    def is_cube(self, e: Entity) -> bool:
        return e.code is not None and e.code in self.cube_codes

    # ───── events ─────

    def handle(self, event: Event, t: float | None = None) -> None:
        t = self.clock() if t is None else t
        with self._lock:
            if isinstance(event, Spawn):
                self._spawn(event, t)
            elif isinstance(event, Despawn):
                self._entities.pop(event.entity_id, None)
            elif isinstance(event, ZoneChange):
                self._entities.clear()
                self.tracker.clear_anchors()
                self._write({"type": "zone", "t": t})

    def _spawn(self, sp: Spawn, t: float) -> None:
        old = self._entities.get(sp.entity_id)
        e = Entity(sp.entity_id, sp.code, sp.kind, sp.pos, old.first_seen if old else t, t)
        self._entities[sp.entity_id] = e
        if sp.pos is not None:
            self.tracker.add_anchor(sp.pos)
        if len(self._entities) > MAX_ENTITIES:
            oldest = min(self._entities.values(), key=lambda x: x.last_seen)
            del self._entities[oldest.entity_id]
        self._write(
            {
                "type": "spawn",
                "t": t,
                "id": sp.entity_id,
                "code": sp.code,
                "kind": sp.kind,
                "pos": sp.pos,
                "self": self.tracker.position,
                "raw": sp.raw.hex(),
            }
        )
        if old is None and self.is_cube(e):
            if e.pos is not None:
                self.spots.add(e.pos, e.code, t)
            if self.on_new_cube:
                self.on_new_cube(e)

    def note_self_position(self) -> None:
        """Called after client packets were fed to the tracker; persists a newly learned key."""
        if self.tracker.locked and not self._saved_selfpos:
            self._saved_selfpos = True
            self.config["self_position"] = self.tracker.key_for_config()
            self.config.save()

    # ───── user actions ─────

    def mark(self) -> bool:
        """The user says "there is a cube right here"; recorded for `analyze`. False without a log."""
        with self._lock:
            self._write({"type": "mark", "t": self.clock(), "self": self.tracker.position})
            return self._log is not None

    def tag_nearest(self) -> Entity | None:
        """Adds the code of the entity nearest to you (or, without a position, the newest) to the cube codes."""
        with self._lock:
            target = self._nearest_untagged()
            if target is None or target.code is None:
                return None
            if target.pos is not None:
                self.spots.add(target.pos, target.code, self.clock())
            codes = list(self.config["cube_codes"])
            if target.code not in codes:
                codes.append(target.code)
                self.config["cube_codes"] = codes
                ignore = [c for c in self.config["ignore_codes"] if c != target.code]
                self.config["ignore_codes"] = ignore
                self.config.save()
            self._write({"type": "tag", "t": self.clock(), "code": target.code, "id": target.entity_id})
            return target

    def _nearest_untagged(self) -> Entity | None:
        candidates = [e for e in self._entities.values() if e.code is not None and not self.is_cube(e)]
        me = self.tracker.position
        if me is not None:
            upm = self.config["units_per_meter"]
            near = [(distance(me, e.pos), e) for e in candidates if e.pos is not None]
            near = [(d, e) for d, e in near if d <= TAG_RADIUS_M * upm]
            if near:
                return min(near, key=lambda x: x[0])[1]
            return None
        return max(candidates, key=lambda e: e.first_seen, default=None)

    def untag(self, code: int) -> None:
        with self._lock:
            self.config["cube_codes"] = [c for c in self.config["cube_codes"] if c != code]
            self.config.save()

    # ───── views ─────

    def snapshot(self, nearby_limit: int = 8) -> Snapshot:
        now = self.clock()
        with self._lock:
            ttl = self.config["cube_ttl_s"]
            for e in [e for e in self._entities.values() if self.is_cube(e) and now - e.last_seen > ttl]:
                del self._entities[e.entity_id]
            me = self.tracker.position
            upm = self.config["units_per_meter"]
            ignore = set(self.config["ignore_codes"])

            def view(e: Entity) -> EntityView:
                d = b = None
                if me is not None and e.pos is not None:
                    d = distance_2d(me, e.pos) / upm
                    b = bearing(me, e.pos)
                return EntityView(e, self.is_cube(e), d, b, now - e.first_seen)

            views = [view(e) for e in self._entities.values()]
            key = lambda v: (v.distance_m if v.distance_m is not None else 1e12, -v.entity.first_seen)
            cubes = sorted((v for v in views if v.is_cube), key=key)
            nearby = sorted(
                (v for v in views if not v.is_cube and v.entity.code not in ignore),
                key=key,
            )[:nearby_limit]
            spots: list[Vec3] = []
            if me is not None:
                radius = self.config["radar_range_m"] * upm * 1.5
                spots = [(s["x"], s["y"], s["z"]) for s in self.spots.near(me, radius)]
            return Snapshot(self.status, me, self.tracker.locked, cubes, nearby, spots, len(self._entities))

    # ───── log ─────

    def _write(self, record: dict) -> None:
        if self._log is None:
            return
        self._log.write(json.dumps(record) + "\n")
        self._log.flush()

    def close(self) -> None:
        if self._log:
            self._log.close()
            self._log = None
