"""Community map data in the running game: which map we are on and which spots are around.

The community data (see ``community.py``) has, for every overworld map, the
3 722 Hidden Cube spots plus thousands of fixed points (gathering nodes,
NPCs, portals, Empyrean Traces...). Everything is in world units, as read from
the game files. This module uses it three ways:

- ``WorldLocator`` finds which map we are on, with no input from the user:
  the entities the server spawns around us sit on that map's fixed points and
  not on the other maps'. It also confirms the axis convention between the
  packets and the data (it is expected to be the identity, but checking costs
  nothing and saves the user a broken radar if it is not).
- ``CubeCodeLearner`` finds the Hidden Cube NPC codes by itself: a code whose
  entities keep appearing exactly on known cube spots is a cube. No more F8/F9.
- ``SpotTracker`` remembers which spots you already passed close enough to see
  and found empty, and which pending spot is the nearest one to check next.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .protocol import Vec3

# Map ids used by the game (and by the data). "key": every cube needs a key;
# "mixed": cubes can be opened free, or with a key for the better reward table.
MAPS: dict[str, tuple[str, str | None]] = {
    "World_L_A": ("Verteron", "key"),
    "World_D_A": ("Altgard", "key"),
    "World_L_B": ("Eltnen", "mixed"),
    "World_D_B": ("Morheim", "mixed"),
    "World_L_Starter": ("Zona inicial elyos", None),
    "World_D_Starter": ("Zona inicial asmodian", None),
}

# An entity "is on" a data point when it is this close (units, 2D) ...
MATCH_RADIUS = 300.0
# ... and its height differs by at most this much.
MATCH_HEIGHT = 500.0
# A cube entity this close to a cube spot (2D) is at that spot.
CUBE_SPOT_RADIUS = 300.0

# Map location: the best (map, axes) pair needs this many matched spawns, and
# this many times more than the runner-up.
LOCATE_MIN_HITS = 6
LOCATE_MARGIN = 3.0
# If heights never match (the server measuring z differently from the data),
# fall back to 2D after this many spawns, with a stricter bar.
LOCATE_2D_AFTER = 150
LOCATE_2D_MIN_HITS = 15

GRID_CELL = 2000.0


# ───── axis conventions ─────

Transform = Callable[[float, float], tuple[float, float]]

# Game coordinates -> data coordinates. "xy" (identity) is the expected one.
TRANSFORMS: dict[str, Transform] = {
    "xy": lambda x, y: (x, y),
    "x-y": lambda x, y: (x, -y),
    "-xy": lambda x, y: (-x, y),
    "-x-y": lambda x, y: (-x, -y),
    "yx": lambda x, y: (y, x),
    "y-x": lambda x, y: (y, -x),
    "-yx": lambda x, y: (-y, x),
    "-y-x": lambda x, y: (-y, -x),
}
INVERSE = {"xy": "xy", "x-y": "x-y", "-xy": "-xy", "-x-y": "-x-y", "yx": "yx", "y-x": "-yx", "-yx": "y-x", "-y-x": "-y-x"}


def to_data(name: str, p: Vec3) -> Vec3:
    x, y = TRANSFORMS[name](p[0], p[1])
    return (x, y, p[2])


def to_game(name: str, p: Vec3) -> Vec3:
    return to_data(INVERSE[name], p)


# ───── spatial index ─────


class GridIndex:
    """Points bucketed in a 2D grid, for fast "anything within r of here?"."""

    def __init__(self, points: Iterable[Vec3], cell: float = GRID_CELL):
        self.cell = cell
        self.points: list[Vec3] = []
        self._grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for p in points:
            self._grid[self._key(p[0], p[1])].append(len(self.points))
            self.points.append((float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0))

    def __len__(self) -> int:
        return len(self.points)

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return (math.floor(x / self.cell), math.floor(y / self.cell))

    def within(self, p: Vec3, radius: float, height: float | None = None) -> list[int]:
        """Indexes of the points within ``radius`` (2D) of p, and within ``height`` vertically if given."""
        cx, cy = self._key(p[0], p[1])
        span = int(math.ceil(radius / self.cell))
        out = []
        r2 = radius * radius
        for gx in range(cx - span, cx + span + 1):
            for gy in range(cy - span, cy + span + 1):
                for i in self._grid.get((gx, gy), ()):
                    q = self.points[i]
                    if (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 > r2:
                        continue
                    if height is not None and abs(q[2] - p[2]) > height:
                        continue
                    out.append(i)
        return out

    def nearest(self, p: Vec3, radius: float, height: float | None = None) -> int | None:
        best, best_d = None, None
        for i in self.within(p, radius, height):
            q = self.points[i]
            d = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
            if best_d is None or d < best_d:
                best, best_d = i, d
        return best


@dataclass
class MapData:
    map_id: str
    cubes: GridIndex
    anchors: GridIndex

    @property
    def name(self) -> str:
        return MAPS.get(self.map_id, (self.map_id, None))[0]

    @property
    def cube_rule(self) -> str | None:
        return MAPS.get(self.map_id, (None, None))[1]


@dataclass
class WorldData:
    maps: dict[str, MapData]
    source: str = ""
    fetched: str = ""

    @classmethod
    def from_dataset(cls, data: dict) -> "WorldData":
        maps = {}
        for map_id, m in data.get("maps", {}).items():
            cubes = [tuple(p) for p in m.get("cubes", [])]
            anchors = [tuple(p) for p in m.get("anchors", [])] + cubes
            if anchors:
                maps[map_id] = MapData(map_id, GridIndex(cubes), GridIndex(anchors))
        return cls(maps, data.get("source", ""), data.get("fetched", ""))

    @property
    def cube_count(self) -> int:
        return sum(len(m.cubes) for m in self.maps.values())


# ───── which map are we on ─────


class WorldLocator:
    """Votes for the (map, axes) pair whose fixed points the spawned entities sit on."""

    def __init__(self, world: WorldData, transform: str | None = None):
        self.world = world
        self.transform: str | None = transform if transform in TRANSFORMS else None
        self.map_id: str | None = None
        # Height tolerance in use: MATCH_HEIGHT, or None once only 2D matched.
        self.height: float | None = MATCH_HEIGHT
        self._scores: dict[tuple[str, str], int] = defaultdict(int)
        self._scores_2d: dict[tuple[str, str], int] = defaultdict(int)
        self.observed = 0

    @property
    def map(self) -> MapData | None:
        return self.world.maps.get(self.map_id) if self.map_id else None

    def reset(self) -> None:
        """Zone change: the map is unknown again (the axes are not)."""
        self.map_id = None
        self._scores.clear()
        self._scores_2d.clear()
        self.observed = 0

    def observe(self, pos: Vec3) -> bool:
        """Feeds an entity position. Returns True when this located the map."""
        if self.map_id is not None:
            return False
        self.observed += 1
        names = [self.transform] if self.transform else list(TRANSFORMS)
        for name in names:
            p = to_data(name, pos)
            for map_id, m in self.world.maps.items():
                near = m.anchors.within(p, MATCH_RADIUS)
                if near:
                    self._scores_2d[(map_id, name)] += 1
                    if any(abs(m.anchors.points[i][2] - p[2]) <= MATCH_HEIGHT for i in near):
                        self._scores[(map_id, name)] += 1
        if self._decide(self._scores, LOCATE_MIN_HITS):
            self.height = MATCH_HEIGHT
            return True
        if self.observed >= LOCATE_2D_AFTER and self._decide(self._scores_2d, LOCATE_2D_MIN_HITS):
            self.height = None
            return True
        return False

    def _decide(self, scores: dict[tuple[str, str], int], min_hits: int) -> bool:
        if not scores:
            return False
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        (map_id, name), best = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        if best >= min_hits and best >= LOCATE_MARGIN * max(second, 1):
            self.map_id = map_id
            self.transform = name
            return True
        return False


# ───── learning the cube NPC codes ─────


@dataclass
class _CodeStats:
    seen_before: int = 0  # entities counted in earlier sessions
    entities: set[int] = field(default_factory=set)  # entity ids this session
    spots: set[tuple[str, int]] = field(default_factory=set)
    maps: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.seen_before + len(self.entities)


class CubeCodeLearner:
    """A code whose entities appear on known cube spots, and rarely elsewhere, is a cube."""

    MIN_SPOTS = 2
    MIN_PRECISION = 0.5
    MAX_SAVED = 200

    def __init__(self, saved: dict | None = None):
        self.stats: dict[int, _CodeStats] = {}
        for code, s in (saved or {}).items():
            try:
                self.stats[int(code)] = _CodeStats(
                    int(s.get("seen", 0)), set(), {(str(m), int(i)) for m, i in s.get("spots", [])}, set(s.get("maps", []))
                )
            except (TypeError, ValueError, AttributeError):
                continue  # a hand-edited entry we cannot read

    def observe(self, code: int, entity_id: int, spot: tuple[str, int] | None) -> None:
        s = self.stats.setdefault(code, _CodeStats())
        s.entities.add(entity_id)
        if spot is not None:
            s.spots.add(spot)
            s.maps.add(spot[0])

    def new_session(self) -> None:
        """Entity ids are only unique within a connection."""
        for s in self.stats.values():
            s.seen_before = s.total
            s.entities.clear()

    def precision(self, code: int) -> float:
        s = self.stats.get(code)
        return len(s.spots) / max(s.total, 1) if s else 0.0

    def is_probable(self, code: int) -> bool:
        """Seen on a cube spot, and (almost) only there."""
        s = self.stats.get(code)
        return bool(s and s.spots and self.precision(code) >= self.MIN_PRECISION)

    def is_confirmed(self, code: int) -> bool:
        s = self.stats.get(code)
        return bool(s and len(s.spots) >= self.MIN_SPOTS and self.precision(code) >= self.MIN_PRECISION)

    def maps_of(self, code: int) -> set[str]:
        s = self.stats.get(code)
        return set(s.maps) if s else set()

    def to_config(self) -> dict:
        """Only codes that hit a spot are worth keeping; the most cube-like first."""
        hits = [(code, s) for code, s in self.stats.items() if s.spots]
        hits.sort(key=lambda cs: -len(cs[1].spots) / max(cs[1].total, 1))
        return {
            str(code): {"seen": s.total, "spots": sorted(s.spots)[:500], "maps": sorted(s.maps)}
            for code, s in hits[: self.MAX_SAVED]
        }


# ───── which spots did we already check ─────


@dataclass
class SpotView:
    pos: Vec3  # game coordinates
    status: str  # "cube", "empty" or "pending"
    distance_m: float | None = None
    bearing_deg: float | None = None


class SpotTracker:
    """Marks cube spots you came close enough to see as empty, until they may have respawned."""

    def __init__(self, check_radius_m: float = 40.0, recheck_s: float = 1800.0, units_per_meter: float = 100.0):
        self.default_radius_m = check_radius_m
        self.recheck_s = recheck_s
        self.upm = units_per_meter
        self.checked: dict[tuple[str, int], float] = {}
        self.first_seen_m: list[float] = []

    @property
    def check_radius_m(self) -> float:
        """How close you must be to a spot to know it is empty.

        The server only sends entities within some radius, unknown and maybe
        varying. Every cube we see appear tells us it was at least that far
        away when it showed up; a fraction of the median is a safe radius.
        """
        if len(self.first_seen_m) < 3:
            return self.default_radius_m
        return max(15.0, min(120.0, 0.6 * statistics.median(self.first_seen_m)))

    def cube_appeared(self, distance_m: float) -> None:
        self.first_seen_m.append(distance_m)
        del self.first_seen_m[:-50]

    def update(self, m: MapData, me_data: Vec3, cube_spots: set[int], t: float) -> None:
        r = self.check_radius_m * self.upm
        for i in m.cubes.within(me_data, r):
            if i not in cube_spots:
                self.checked[(m.map_id, i)] = t

    def status(self, map_id: str, i: int, cube_spots: set[int], t: float) -> str:
        if i in cube_spots:
            return "cube"
        when = self.checked.get((map_id, i))
        return "empty" if when is not None and t - when < self.recheck_s else "pending"

    def forget(self, map_id: str, i: int) -> None:
        self.checked.pop((map_id, i), None)
