"""World state: which entities are around us, and which of them are Hidden Cubes."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from .config import Config
from .diag import Diagnostics, log
from .protocol import Despawn, EntityMove, Event, MapLoad, SelfIdentity, Spawn, Vec3, ZoneChange
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
# Your own entity: a position update this close (units) to where you just teleported ...
SELF_MATCH_UNITS = 1500.0
# ... within this many seconds of the teleport, twice.
SELF_MATCH_S = 20.0
# A teleport position stays good this long; after it, without updates of your
# own entity, your position is estimated from the entities around you.
TELEPORT_FRESH_S = 8.0
APPROX_WINDOW_S = 30.0
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
    world: str = ""
    self_source: str | None = None  # "servidor", "teletransporte", "aproximada" or "cliente"  # state of the community data, for the status line
    map_id: str | None = None
    map_name: str | None = None
    cube_rule: str | None = None  # "key", "mixed" or None
    next_spot: SpotView | None = None  # nearest spot still to check
    pending_near: int = 0  # spots still to check within the radar range


def bearing(src: Vec3, dst: Vec3) -> float:
    """Compass bearing from src to dst (0 = north, clockwise), as on the game's map.

    The game's map (and aion2.th.gl, whose ``fromInGameCoords`` turns an in-game
    (x, y) into map (y, x) drawn with y growing downwards) has +X to the east and
    +Y to the south. ``radar_rotation_deg`` still turns it if needed."""
    return math.degrees(math.atan2(dst[0] - src[0], -(dst[1] - src[1]))) % 360


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
        self.diag = Diagnostics()
        # Where you are, from the server (the client's packets are encrypted).
        self.self_source: str | None = None
        self.self_id: int | None = None
        self.map_number: int | None = None
        self._teleport: tuple[Vec3, float] | None = None
        self._self_t = 0.0
        self._self_candidates: dict[int, int] = {}
        self._recent: deque[tuple[float, Vec3]] = deque(maxlen=400)

    @property
    def entity_count(self) -> int:
        return len(self._entities)

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
                    self._locate(e.pos, e.code)

    @property
    def current_map(self) -> MapData | None:
        return self.locator.map if self.locator else None

    def _locate(self, pos: Vec3, code: int | None = None) -> None:
        if self.locator is None or not self.locator.observe(pos, code):
            return
        m = self.locator.map
        if self.map_number is not None:
            known = dict(self.config["map_ids"])
            if known.get(str(self.map_number)) != m.map_id:
                known[str(self.map_number)] = m.map_id
                self.config["map_ids"] = known
        self.world_status = f"Mapa: {m.name} ({len(m.cubes)} spots)"
        log.info("Mapa reconocido: %s (ejes %s, altura %s)", m.map_id, self.locator.transform, self.locator.height is not None)
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
            log.info("Código de Hidden Cube aprendido: %s", e.code)
            if self.on_learned:
                self.on_learned(e.code)
        self.config.save()

    # ───── events ─────

    def handle(self, event: Event, t: float | None = None) -> None:
        t = self.clock() if t is None else t
        with self._lock:
            if isinstance(event, Spawn):
                self.diag.spawn(event.code, event.pos)
                self._spawn(event, t)
            elif isinstance(event, Despawn):
                self.diag.count("despawns")
                self._entities.pop(event.entity_id, None)
            elif isinstance(event, ZoneChange):
                # The map vote is not reset here: it follows a sliding window (see WorldLocator).
                self.diag.zone(t)
                self._entities.clear()
                self.tracker.clear_anchors()
                self._recent.clear()
                if event.pos is not None:
                    self._teleport = (event.pos, t)
                    self.self_id = None
                    self._self_candidates.clear()
                    self._set_self(event.pos, "teletransporte", t)
                self._write({"type": "zone", "t": t, "pos": event.pos})
            elif isinstance(event, EntityMove):
                self._move(event, t)
            elif isinstance(event, MapLoad):
                self._map_load(event.map_id, t)
                if event.pos is not None:
                    self._teleport = (event.pos, t)
                    self._set_self(event.pos, "teletransporte", t)
            elif isinstance(event, SelfIdentity):
                if event.entity_id != self.self_id:
                    log.info("Tu entidad (registro propio): id %s", event.entity_id)
                self.self_id = event.entity_id

    # ───── where you are ─────

    @property
    def self_known(self) -> bool:
        return self.tracker.position is not None and (self.tracker.locked or self.self_source is not None)

    @property
    def self_precise(self) -> bool:
        """Good enough to mark spots as checked (not an estimate, not an old teleport)."""
        if self.tracker.locked or self.self_source == "servidor":
            return True
        return self.self_source == "teletransporte" and self._teleport is not None and self.clock() - self._teleport[1] < TELEPORT_FRESH_S

    def _set_self(self, pos: Vec3, source: str, t: float) -> None:
        self.tracker.position = pos
        self.tracker.updated_at = t
        self.self_source = source
        self._self_t = t

    def _move(self, mv: EntityMove, t: float) -> None:
        if mv.entity_id == self.self_id:
            self.diag.count("self_moves")
            self._set_self(mv.pos, "servidor", t)
            return
        self.diag.count("moves")
        self._recent.append((t, mv.pos))
        e = self._entities.get(mv.entity_id)
        if e is not None:
            e.pos = mv.pos
            e.last_seen = t
            return
        # Not a spawned entity: maybe you. Your own updates start where you teleported.
        if self._teleport is not None and t - self._teleport[1] <= SELF_MATCH_S:
            if distance_2d(mv.pos, self._teleport[0]) <= SELF_MATCH_UNITS:
                n = self._self_candidates.get(mv.entity_id, 0) + 1
                self._self_candidates[mv.entity_id] = n
                if n >= 2:
                    self.self_id = mv.entity_id
                    log.info("Tu entidad: id %s", mv.entity_id)
                    self._set_self(mv.pos, "servidor", t)

    def _map_load(self, number: int, t: float) -> None:
        self.diag.count("map_loads")
        self._write({"type": "map_load", "t": t, "map": number})
        if number == self.map_number:
            return
        log.info("Carga de mapa: id %s", number)
        self.map_number = number
        if self.locator is None:
            return
        self.locator.reset()
        known = self.config["map_ids"].get(str(number))
        if known in self.world.maps:
            self.locator.map_id = known
            self.locator.transform = self.locator.transform or "yx"
            m = self.locator.map
            self.world_status = f"Mapa: {m.name} ({len(m.cubes)} spots)"
        else:
            self.world_status = f"{self.world.cube_count} spots de {self.world.source} · buscando mapa"

    def _estimate_self(self, now: float) -> None:
        """Without a fresh position from the server, the middle of what the server shows you."""
        if self.tracker.locked:
            self.self_source = "cliente"
            return
        if self.self_source in ("servidor", "cliente") and now - self._self_t < 10:
            return
        if self.self_source == "teletransporte" and self._teleport and now - self._teleport[1] < TELEPORT_FRESH_S:
            return
        pts = [p for tt, p in self._recent if now - tt <= APPROX_WINDOW_S]
        pts += [e.pos for e in self._entities.values() if e.pos is not None and now - e.last_seen <= APPROX_WINDOW_S]
        if len(pts) < 5:
            return
        xs, ys, zs = sorted(p[0] for p in pts), sorted(p[1] for p in pts), sorted(p[2] for p in pts)
        mid = len(pts) // 2
        self._set_self((xs[mid], ys[mid], zs[mid]), "aproximada", self._self_t)

    def new_connection(self, t: float | None = None) -> None:
        """A new game connection: entity ids start over, and so may the map."""
        with self._lock:
            self.learner.new_session()
            self.handle(ZoneChange(), t)
            self.map_number = None
            self.self_id = None
            if self.locator:
                self.locator.reset()
                self.world_status = f"{self.world.cube_count} spots de {self.world.source} · buscando mapa"

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
            self._locate(sp.pos, sp.code)
            self._recent.append((t, sp.pos))
        if old is None:
            self._learn(e)
        if old is None and self.is_cube(e):
            if e.pos is not None:
                self.spots.add(e.pos, e.code, t)
                me = self.tracker.position
                if me is not None and self.self_precise:
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
            self._estimate_self(now)
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
            snap = Snapshot(self.status, me, self.self_known, cubes, nearby, [], len(self._entities), self.world_status, self.self_source)
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
        if self.self_precise:
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
