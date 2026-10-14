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
from .worldmap import (
    CUBE_SPOT_RADIUS,
    CubeCodeLearner,
    MapData,
    SpotTracker,
    SpotView,
    WorldData,
    WorldLocator,
    to_data,
    to_game,
)

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
    kind: str | None = None  # "key", "free" or None (unknown), for cubes


@dataclass
class Snapshot:
    status: str
    self_pos: Vec3 | None
    self_learned: bool
    cubes: list[EntityView]
    nearby: list[EntityView]
    spots: list[SpotView]  # known spots around you, in game coordinates
    entity_count: int
    world: str = ""  # state of the community data, for the status line
    map_id: str | None = None
    map_name: str | None = None
    cube_rule: str | None = None  # "key", "mixed" or None
    next_spot: SpotView | None = None  # nearest spot still to check
    pending_near: int = 0  # spots still to check within the radar range


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
        self.world: WorldData | None = None
        self.world_status = "Sin spots de la comunidad"
        self.locator: WorldLocator | None = None
        self.learner = CubeCodeLearner(config["cube_candidates"])
        self.spot_tracker = SpotTracker(
            config["spot_check_radius_m"], config["spot_recheck_s"], config["units_per_meter"]
        )
        self.on_learned: Callable[[int], None] | None = None

    # ───── codes ─────

    @property
    def cube_codes(self) -> set[int]:
        return set(self.config["cube_codes"])

    def is_cube(self, e: Entity) -> bool:
        if e.code is None:
            return False
        if e.code in self.cube_codes:
            return True
        return bool(self.config["auto_cube_codes"]) and self.learner.is_probable(e.code)

    def cube_kind(self, code: int | None) -> str | None:
        kind = self.config["cube_kinds"].get(str(code))
        return kind if kind in ("key", "free") else None

    # ───── community data ─────

    def set_world(self, world: WorldData | None) -> None:
        """Installs the community spots (from any thread)."""
        with self._lock:
            self.world = world
            if world is None or not world.maps:
                self.locator = None
                self.world_status = "Sin spots de la comunidad"
                return
            self.locator = WorldLocator(world, self.config["world_transform"])
            self.world_status = f"{world.cube_count} spots de {world.source} · buscando mapa"
            for e in list(self._entities.values()):
                if e.pos is not None:
                    self._locate(e.pos)

    @property
    def current_map(self) -> MapData | None:
        return self.locator.map if self.locator else None

    def _locate(self, pos: Vec3) -> None:
        if self.locator is None or not self.locator.observe(pos):
            return
        m = self.locator.map
        self.world_status = f"Mapa: {m.name} ({len(m.cubes)} spots)"
        if self.config["world_transform"] != self.locator.transform:
            self.config["world_transform"] = self.locator.transform
            self.config.save()
        self._write({"type": "map", "t": self.clock(), "map": m.map_id, "transform": self.locator.transform})
        for e in list(self._entities.values()):
            self._learn(e)

    def _cube_spot(self, pos: Vec3) -> int | None:
        m = self.current_map
        if m is None:
            return None
        return m.cubes.nearest(to_data(self.locator.transform, pos), CUBE_SPOT_RADIUS, self.locator.height)

    def _learn(self, e: Entity) -> None:
        """Evidence for or against e's code being a Hidden Cube."""
        m = self.current_map
        if m is None or e.code is None or e.pos is None or e.code in set(self.config["ignore_codes"]):
            return
        spot = self._cube_spot(e.pos)
        self.learner.observe(e.code, e.entity_id, (m.map_id, spot) if spot is not None else None)
        if spot is None:
            return
        if m.cube_rule == "key" and self.cube_kind(e.code) is None and self.learner.is_probable(e.code):
            kinds = dict(self.config["cube_kinds"])
            kinds[str(e.code)] = "key"
            self.config["cube_kinds"] = kinds
        self.config["cube_candidates"] = self.learner.to_config()
        if (
            self.config["auto_cube_codes"]
            and e.code not in self.cube_codes
            and self.learner.is_confirmed(e.code)
        ):
            self.config["cube_codes"] = list(self.config["cube_codes"]) + [e.code]
            self._write({"type": "learned", "t": self.clock(), "code": e.code})
            if self.on_learned:
                self.on_learned(e.code)
        self.config.save()

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
                if self.locator:
                    self.locator.reset()
                    self.world_status = f"{self.world.cube_count} spots de {self.world.source} · buscando mapa"
                self._write({"type": "zone", "t": t})

    def new_connection(self, t: float | None = None) -> None:
        """A new game connection: entity ids start over, and so may the map."""
        with self._lock:
            self.learner.new_session()
            self.handle(ZoneChange(), t)

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
        if sp.pos is not None:
            self._locate(sp.pos)
        if old is None:
            self._learn(e)
        if old is None and self.is_cube(e):
            if e.pos is not None:
                self.spots.add(e.pos, e.code, t)
                me = self.tracker.position
                if me is not None and self.tracker.locked:
                    self.spot_tracker.cube_appeared(distance_2d(me, e.pos) / self.config["units_per_meter"])
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

    def reset_spots(self) -> int:
        """Forgets which spots were checked (after a channel change, say). Returns how many."""
        with self._lock:
            n = len(self.spot_tracker.checked)
            self.spot_tracker.checked.clear()
            return n

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
                cube = self.is_cube(e)
                return EntityView(e, cube, d, b, now - e.first_seen, self.cube_kind(e.code) if cube else None)

            views = [view(e) for e in self._entities.values()]
            key = lambda v: (v.distance_m if v.distance_m is not None else 1e12, -v.entity.first_seen)
            cubes = sorted((v for v in views if v.is_cube), key=key)
            nearby = sorted(
                (v for v in views if not v.is_cube and v.entity.code not in ignore),
                key=key,
            )[:nearby_limit]
            snap = Snapshot(self.status, me, self.tracker.locked, cubes, nearby, [], len(self._entities), self.world_status)
            m = self.current_map
            if m is not None:
                snap.map_id, snap.map_name, snap.cube_rule = m.map_id, m.name, m.cube_rule
            if me is None:
                return snap
            radius = self.config["radar_range_m"] * upm * 1.5
            if m is None:
                snap.spots = [SpotView((s["x"], s["y"], s["z"]), "learned") for s in self.spots.near(me, radius)]
                return snap
            self._community_spots(snap, m, me, cubes, radius, now)
            return snap

    def _community_spots(self, snap: Snapshot, m: MapData, me: Vec3, cubes: list[EntityView], radius: float, now: float) -> None:
        tf = self.locator.transform
        upm = self.config["units_per_meter"]
        me_d = to_data(tf, me)
        occupied = {i for v in cubes if v.entity.pos is not None and (i := self._cube_spot(v.entity.pos)) is not None}
        if self.tracker.locked:
            self.spot_tracker.update(m, me_d, occupied, now)
        best: tuple[float, int] | None = None
        for i, p in enumerate(m.cubes.points):
            d2 = (p[0] - me_d[0]) ** 2 + (p[1] - me_d[1]) ** 2
            status = self.spot_tracker.status(m.map_id, i, occupied, now)
            if d2 <= radius * radius:
                g = to_game(tf, p)
                snap.spots.append(SpotView(g, status, math.sqrt(d2) / upm, bearing(me, g)))
                if status == "pending" and d2 <= (self.config["radar_range_m"] * upm) ** 2:
                    snap.pending_near += 1
            if status == "pending" and (best is None or d2 < best[0]):
                best = (d2, i)
        if best is not None:
            g = to_game(tf, m.cubes.points[best[1]])
            snap.next_spot = SpotView(g, "pending", math.sqrt(best[0]) / upm, bearing(me, g))

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
