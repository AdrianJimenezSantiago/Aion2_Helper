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
from .protocol import (
    Despawn,
    EntityFacing,
    EntityMove,
    EntityVelocity,
    Event,
    MapLoad,
    PlayerSpawn,
    SelfIdentity,
    SelfStats,
    Spawn,
    Vec3,
    ZoneChange,
)
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
# A learned movement opcode's position further than this from the entity's known position is noise.
LEARNED_MOVE_MAX_JUMP = 5000.0
# Faster than this between two of your records (units/s) is a misread, not you (flying is ~16 m/s).
SELF_MAX_SPEED = 5000.0
# Your own entity: a position update this close (units) to where you just teleported ...
SELF_MATCH_UNITS = 1500.0
# ... within this many seconds of the teleport, twice.
SELF_MATCH_S = 20.0
# A teleport position stays good this long; after it, without updates of your
# own entity, your position is estimated from the entities around you.
TELEPORT_FRESH_S = 8.0
APPROX_WINDOW_S = 30.0
# Entities come into and leave your view on a circle around you: its centre is where you are,
# 20-30 m off. Spawns right after a zone load fill the whole view and are left out.
EDGE_WINDOW_S = 20.0
EDGE_MIN_POINTS = 6
EDGE_SETTLE_S = 3.0
EDGE_RADIUS_RANGE = (1500.0, 30000.0)
# A skill you aim at a target puts you near it, about 10 m off.
NEAR_TARGET_S = 10.0
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
    self_source: str | None = None  # "servidor", "teletransporte", "aproximada" or "cliente"
    heading_deg: float | None = None  # where you are walking (compass), when you move
    velocity: tuple[float, float] | None = None  # units/s on x, y, for smooth drawing between records
    pos_time: float = 0.0  # when self_pos was reported  # state of the community data, for the status line
    map_id: str | None = None
    map_name: str | None = None
    cube_rule: str | None = None  # "key", "mixed" or None
    next_spot: SpotView | None = None  # nearest spot still to check
    pending_near: int = 0  # spots still to check within the radar range
    self_identified: bool = False  # your entity is known for sure (4a 36 or the 33 36 login record)


def bearing(src: Vec3, dst: Vec3) -> float:
    """Compass bearing from src to dst (0 = north, clockwise), as on the game's map.

    The game's map (and aion2.th.gl, whose ``fromInGameCoords`` turns an in-game
    (x, y) into map (y, x) drawn with y growing downwards) has +X to the east and
    +Y to the south. ``radar_rotation_deg`` still turns it if needed."""
    return math.degrees(math.atan2(dst[0] - src[0], -(dst[1] - src[1]))) % 360


def fit_circle(points: list[Vec3]) -> tuple[float, float, float] | None:
    """Centre and radius of the circle through ``points`` (x, y), by least squares (Kasa).

    None when they do not lie on a plausible view circle all around a centre."""
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    # x^2 + y^2 + a x + b y + c = 0, in coordinates centred on the mean for stability
    sxx = sxy = syy = sx = sy = szx = szy = sz = 0.0
    for p in points:
        x, y = p[0] - mx, p[1] - my
        z = x * x + y * y
        sxx += x * x
        sxy += x * y
        syy += y * y
        sx += x
        sy += y
        szx += z * x
        szy += z * y
        sz += z
    m = [[sxx, sxy, sx, -szx], [sxy, syy, sy, -szy], [sx, sy, float(n), -sz]]
    for i in range(3):  # Gaussian elimination with partial pivoting
        k = max(range(i, 3), key=lambda r: abs(m[r][i]))
        if abs(m[k][i]) < 1e-9:
            return None
        m[i], m[k] = m[k], m[i]
        for r in range(3):
            if r != i:
                f = m[r][i] / m[i][i]
                m[r] = [a - f * b for a, b in zip(m[r], m[i])]
    a, b, c = (m[i][3] / m[i][i] for i in range(3))
    cx, cy = -a / 2, -b / 2
    r2 = cx * cx + cy * cy - c
    if r2 <= 0:
        return None
    r = math.sqrt(r2)
    if not (EDGE_RADIUS_RANGE[0] <= r <= EDGE_RADIUS_RANGE[1]):
        return None
    rms = math.sqrt(sum((math.hypot(p[0] - mx - cx, p[1] - my - cy) - r) ** 2 for p in points) / n)
    angles = sorted(math.atan2(p[1] - my - cy, p[0] - mx - cx) for p in points)
    gaps = [b - a for a, b in zip(angles, angles[1:])] + [angles[0] + 2 * math.pi - angles[-1]]
    if rms > 0.15 * r or max(gaps) > math.pi:
        return None  # not a circle, or only an arc of it: the centre is not pinned down
    return cx + mx, cy + my, r


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
        self.tracker.diag = self.diag
        self._trail: deque[tuple[float, Vec3]] = deque(maxlen=60)  # precise positions, for your heading
        # Where you are, from the server (the client's packets are encrypted).
        self.self_source: str | None = None
        self.self_id: int | None = None
        self.map_number: int | None = None
        self._teleport: tuple[Vec3, float] | None = None
        self._self_t = 0.0
        self._self_candidates: dict[int, int] = {}
        self._self_hint: int | None = None
        # self_id came from a record only ever about you (4a 36 stats, 33 36 login): no scoring may change it
        self._self_confirmed = False
        self._others: set[int] = set()  # other players: never you
        # Every id that ever spawned on this connection (monsters, objects, pets, players): never you.
        # Not cleared at a zone change, whose bundle can carry your pet's spawn just before it.
        self._spawned_ids: set[int] = set()
        self._self_vel: tuple[float, float, float] | None = None
        self._self_yaw: tuple[float, float] | None = None  # (time, yaw degrees) of your last record
        self._moving = False
        self._speed: float | None = None  # units/s, measured between your records
        self._last_fix: tuple[float, Vec3] | None = None
        # Dead reckoning: your last known point, carried along by every velocity record of yours
        # (1d 37 ten times a second, 20 37 and 29 37 when flying) until the next fix.
        self._dr: tuple[float, Vec3] | None = None
        self._dr_vel: tuple[float, float, float, float] | None = None  # (time, vx, vy, vz)
        self._recent: deque[tuple[float, Vec3]] = deque(maxlen=400)
        self._edges: deque[tuple[float, Vec3]] = deque(maxlen=64)  # where entities came into or left your view
        self._zone_t = -1e9
        self._near_target: tuple[float, Vec3] | None = None  # the target of your last aimed skill

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
                gone = self._entities.pop(event.entity_id, None)
                if event.flag == 1 and gone is not None and gone.pos is not None:
                    self._edges.append((t, gone.pos))  # it left your view
            elif isinstance(event, ZoneChange):
                # The map vote is not reset here: it follows a sliding window (see WorldLocator).
                self.diag.zone(t)
                self._entities.clear()
                self.tracker.clear_anchors()
                self._recent.clear()
                self._edges.clear()
                self._near_target = None
                self._zone_t = t
                if event.pos is not None:
                    self.diag.note(f"teletransporte a ({event.pos[0]:.0f}, {event.pos[1]:.0f}, {event.pos[2]:.0f})")
                    self._teleport = (event.pos, t)
                    self._self_candidates.clear()  # a new zone: your entity is the one moving from here
                    self._set_self(event.pos, "teletransporte", t)
                self._write({"type": "zone", "t": t, "pos": event.pos})
            elif isinstance(event, EntityMove):
                self._move(event, t)
            elif isinstance(event, EntityVelocity):
                if event.entity_id == self.self_id:
                    self._velocity_update(event, t)
            elif isinstance(event, MapLoad):
                self._map_load(event.map_id, t)
                if event.pos is not None:
                    self._teleport = (event.pos, t)
                    self._self_candidates.clear()  # a new zone: your entity is the one moving from here
                    self._set_self(event.pos, "teletransporte", t)
            elif isinstance(event, PlayerSpawn):
                # Not excluded from being you: the zone load seems to spawn your own character this way too.
                self._others.add(event.entity_id)
                if len(self._others) > 5000:
                    self._others.clear()
            elif isinstance(event, SelfStats):
                self._confirm_self(event.entity_id, "estadísticas 4a 36")
            elif isinstance(event, SelfIdentity):
                if event.confirmed:
                    # The login record, name and all: sent only when you enter the world, which is
                    # why the tool has to be running before you pick your character.
                    self._confirm_self(event.entity_id, "entrada al mundo 33 36")
                else:
                    # A bare 33 36 is only a hint (a live session got 16329 there, 2348 in the moves).
                    self._self_hint = event.entity_id
            elif isinstance(event, EntityFacing):
                if event.entity_id == self.self_id:
                    self.diag.count("self_facings")
                    self._self_yaw = (t, event.yaw)
                    if event.target is not None and event.target != self.self_id:
                        aimed = self._entities.get(event.target)
                        pos = event.target_pos or (aimed.pos if aimed is not None else None)
                        if pos is not None:
                            self._near_target = (t, pos)

    # ───── where you are ─────

    @property
    def self_identified(self) -> bool:
        """Your entity came from a record only about you, not from guessing."""
        return self._self_confirmed

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
        if source != "aproximada":
            self._trail.append((t, pos))
            self._dr = (t, pos)
        if source == "teletransporte":
            self.tracker.add_anchor(pos)  # where the client must report you, too

    def velocity(self, now: float) -> tuple[float, float] | None:
        """Your speed (units/s): the one your own 29 37 record carries if fresh, else from the last
        two reports. Lets the radar glide between them."""
        if self._dr_vel is not None and now - self._dr_vel[0] <= 0.5:
            return self._dr_vel[1], self._dr_vel[2]
        if self._self_vel is not None and now - self._self_vel[0] <= 1.5:
            return self._self_vel[1], self._self_vel[2]
        if self._self_yaw is not None:
            # Walking along your facing at your measured speed, until a stop record (1a 37).
            if not self._moving or now - self._self_yaw[0] > 4.0 or self._speed is None:
                return None
            a = math.radians(self._self_yaw[1])
            return self._speed * math.cos(a), self._speed * math.sin(a)
        if len(self._trail) < 2:
            return None
        (t1, a), (t2, b) = self._trail[-2], self._trail[-1]
        if now - t2 > 3.0 or not (0.05 <= t2 - t1 <= 3.0):
            return None
        vx, vy = (b[0] - a[0]) / (t2 - t1), (b[1] - a[1]) / (t2 - t1)
        if (vx * vx + vy * vy) ** 0.5 > 3000:  # faster than any mount: a jump in the data
            return None
        return vx, vy

    def _velocity_update(self, v: EntityVelocity, t: float) -> None:
        self.diag.count("self_velocities")
        self._advance(t)
        self._dr_vel = (t, v.vx, v.vy, v.vz or 0.0)
        self._self_vel = (t, v.vx, v.vy)
        self._moving = bool(v.vx or v.vy)
        if v.yaw is not None:
            self._self_yaw = (t, v.yaw)
        self._self_t = max(self._self_t, t)

    def _advance(self, t: float) -> None:
        """Moves the dead-reckoned point to time t at the current velocity."""
        if self._dr is None or self._dr_vel is None:
            return
        t0, p = self._dr
        dt = min(max(0.0, t - t0), 1.5)
        if t - self._dr_vel[0] > 1.0:  # no velocity record for a second: you stopped
            dt = min(dt, max(0.0, self._dr_vel[0] + 1.0 - t0))
        _, vx, vy, vz = self._dr_vel
        self._dr = (t, (p[0] + vx * dt, p[1] + vy * dt, p[2] + vz * dt))

    def _dr_estimate(self, now: float) -> Vec3 | None:
        if self._dr is None:
            return None
        self._advance(now)
        return self._dr[1]

    def heading(self, now: float) -> float | None:
        """Where you face (from your last record's yaw), else the bearing of your last couple of
        seconds of movement, if you moved more than 1.5 m."""
        if self._self_yaw is not None and now - self._self_yaw[0] <= 15.0:
            a = math.radians(self._self_yaw[1])
            return bearing((0.0, 0.0, 0.0), (math.cos(a), math.sin(a), 0.0))
        recent = [(t, p) for t, p in self._trail if now - t <= 2.5]
        if len(recent) < 2:
            return None
        a, b = recent[0][1], recent[-1][1]
        if distance_2d(a, b) < 150:
            return None
        return bearing(a, b)

    def _confirm_self(self, eid: int, source: str) -> None:
        """A record only ever about you names your entity for sure."""
        self._self_confirmed = True
        if eid == self.self_id:
            return
        log.info("Tu entidad (%s): id %s", source, eid)
        self.diag.note(f"tu entidad: {eid} ({source})")
        self.self_id = eid
        self._self_candidates.clear()
        self._dr = self._dr_vel = None
        self._last_fix = None

    def _self_update(self, mv: EntityMove, t: float) -> None:
        """One of your own movement records: position, and what it says about where you are heading."""
        last = self._last_fix
        if last is not None and 0 < t - last[0] <= 5.0 and distance_2d(last[1], mv.pos) / (t - last[0]) > SELF_MAX_SPEED:
            self.diag.count("moves_rejected")
            return
        if last is not None and self._moving and 0.2 <= t - last[0] <= 5.0:
            measured = distance_2d(last[1], mv.pos) / (t - last[0])
            if 100 <= measured <= 3000:
                self._speed = measured if self._speed is None else 0.5 * self._speed + 0.5 * measured
        self._last_fix = (t, mv.pos)
        self._set_self(mv.pos, "servidor", t)
        if mv.velocity is not None:
            self._self_vel = (t, mv.velocity[0], mv.velocity[1])
            self._dr_vel = (t, mv.velocity[0], mv.velocity[1], mv.velocity[2])
            self._moving = True
        if mv.yaw is not None:
            self._self_yaw = (t, mv.yaw)
        if mv.phase == "stop":
            self._moving = False
            self._self_vel = None
            self._dr_vel = None
        elif mv.phase in ("start", "move"):
            self._moving = True

    @property
    def self_candidates(self) -> dict[int, int]:
        return dict(self._self_candidates)

    def _plausible(self, mv: EntityMove) -> bool:
        if not mv.learned:
            return True
        known = self.tracker.position if mv.entity_id == self.self_id else None
        e = self._entities.get(mv.entity_id)
        if e is not None and e.pos is not None:
            known = e.pos
        return known is None or distance_2d(known, mv.pos) <= LEARNED_MOVE_MAX_JUMP

    def _move(self, mv: EntityMove, t: float) -> None:
        if not self._plausible(mv):
            self.diag.count("moves_rejected")
            return
        if mv.entity_id == self.self_id or (self.self_id is None and mv.entity_id == self._self_hint):
            self.diag.count("self_moves")
            self._self_update(mv, t)
            return
        self.diag.count("moves")
        self._recent.append((t, mv.pos))
        e = self._entities.get(mv.entity_id)
        if e is not None:
            e.pos = mv.pos
            e.last_seen = t
            return
        if mv.entity_id in self._spawned_ids:
            return
        # Moving, yet never spawned as a monster, object or pet: maybe you; moving where you just
        # teleported counts more. Flags 05 is not a sign: other players walking near you carry it
        # too (a live session flipped between five entities with it). The sure sign is 4a 36.
        score = 1
        if self._teleport is not None and t - self._teleport[1] <= SELF_MATCH_S:
            if distance_2d(mv.pos, self._teleport[0]) <= SELF_MATCH_UNITS:
                score += 2
        n = self._self_candidates.get(mv.entity_id, 0) + score
        self._self_candidates[mv.entity_id] = n
        ranked = sorted(self._self_candidates.values(), reverse=True)
        second = ranked[1] if len(ranked) > 1 else 0
        settled = self._self_confirmed or (
            self.self_id is not None and self.self_source == "servidor" and t - self._self_t < 10
        )
        if not settled and n == ranked[0] and n >= 10 and n >= 3 * second:
            self.self_id = mv.entity_id
            log.info("Tu entidad: id %s", mv.entity_id)
            self.diag.note(f"tu entidad: {mv.entity_id} (puntuación)")
            self._self_update(mv, t)

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
        # Your own entity's records come when you start, turn or stop: hold the last one a while.
        if self.self_source in ("servidor", "cliente") and now - self._self_t < 60:
            return
        if self.self_source == "teletransporte" and self._teleport and now - self._teleport[1] < TELEPORT_FRESH_S:
            return
        if self._near_target is not None and now - self._near_target[0] <= NEAR_TARGET_S:
            self._set_self(self._near_target[1], "aproximada", self._self_t)
            return
        edges = [p for tt, p in self._edges if now - tt <= EDGE_WINDOW_S]
        fit = fit_circle(edges) if len(edges) >= EDGE_MIN_POINTS else None
        if fit is not None:
            zs = sorted(p[2] for p in edges)
            self._set_self((fit[0], fit[1], zs[len(zs) // 2]), "aproximada", self._self_t)
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
            self._self_confirmed = False
            self._self_yaw = None
            self._moving = False
            self._last_fix = None
            self._dr = None
            self._dr_vel = None
            self._spawned_ids.clear()
            self._others.clear()
            if self.locator:
                self.locator.reset()
                self.world_status = f"{self.world.cube_count} spots de {self.world.source} · buscando mapa"

    def _spawn(self, sp: Spawn, t: float) -> None:
        if sp.entity_id != self.self_id:
            self._spawned_ids.add(sp.entity_id)
            if len(self._spawned_ids) > 50000:
                self._spawned_ids.clear()
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
                "owner": sp.owner,
                "pos": sp.pos,
                "self": self.tracker.position,
                "raw": sp.raw.hex(),
            }
        )
        if sp.pos is not None:
            self._locate(sp.pos, sp.code)
            self._recent.append((t, sp.pos))
            if old is None and t - self._zone_t > EDGE_SETTLE_S:
                self._edges.append((t, sp.pos))  # it came into your view
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
        if self.tracker.locked and self.tracker.position is not None:
            t = self.tracker.updated_at
            if not self._trail or self._trail[-1][0] != t:
                self._trail.append((t, self.tracker.position))
            self.self_source = "cliente"
            self._self_t = t
        if self.tracker.locked and not self._saved_selfpos:
            log.info("Posición propia aprendida del cliente: %s", self.tracker.key_for_config())
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
            pos_time = self._self_t
            if self.self_source == "servidor":
                est = self._dr_estimate(now)
                if est is not None:
                    me, pos_time = est, now
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
            precise = self.self_source != "aproximada"
            snap = Snapshot(self.status, me, self.self_known, cubes, nearby, [], len(self._entities), self.world_status,
                            self.self_source, self.heading(now) if precise else None,
                            self.velocity(now) if precise else None, pos_time)  # fmt: skip
            snap.self_identified = self._self_confirmed
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
