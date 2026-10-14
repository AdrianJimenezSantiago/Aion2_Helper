"""Known Hidden Cube spots: places a cube has been seen, kept across sessions.

Cubes rotate between a fixed set of candidate spots, so every cube the overlay
detects is also a spot worth re-checking later. Spots live in world units (the
same Unreal coordinates the game sends), one JSON object per spot:

    {"x": 74914.8, "y": -84788.4, "z": 8853.0, "code": 2920548, "seen": 3, "last": 1760000000.0}

Spots from elsewhere can be merged in with ``python -m aion2_helper import-spots``
(CSV ``x,y,z`` or a JSON list of objects with x, y, z), as long as they are in
world units. Coordinates read off web maps are map pixels and must be converted first.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from .protocol import Vec3
from .selfpos import distance

MERGE_RADIUS = 300.0  # units (3 m): two sightings this close are the same spot


class SpotStore:
    def __init__(self, path: Path | None):
        self.path = path
        self.spots: list[dict] = []
        if path and path.exists():
            with open(path, encoding="utf-8") as f:
                self.spots = json.load(f)

    def add(self, pos: Vec3, code: int | None, t: float) -> bool:
        """Records a sighting; returns True when it is a new spot."""
        for s in self.spots:
            if distance((s["x"], s["y"], s["z"]), pos) <= MERGE_RADIUS:
                s["seen"] = s.get("seen", 0) + 1
                s["last"] = t
                self.save()
                return False
        self.spots.append({"x": pos[0], "y": pos[1], "z": pos[2], "code": code, "seen": 1, "last": t})
        self.save()
        return True

    def near(self, pos: Vec3, radius: float) -> list[dict]:
        return [s for s in self.spots if distance((s["x"], s["y"], s["z"]), pos) <= radius]

    def merge_file(self, path: Path) -> int:
        added = 0
        for x, y, z in _read_points(path):
            if not any(distance((s["x"], s["y"], s["z"]), (x, y, z)) <= MERGE_RADIUS for s in self.spots):
                self.spots.append({"x": x, "y": y, "z": z, "code": None, "seen": 0, "last": 0})
                added += 1
        self.save()
        return added

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.spots, f, indent=1)
        os.replace(tmp, self.path)


def _read_points(path: Path) -> list[Vec3]:
    if path.suffix.lower() == ".json":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [(float(p["x"]), float(p["y"]), float(p.get("z", 0.0))) for p in data]
    points = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            try:
                x, y = float(row[0]), float(row[1])
            except (ValueError, IndexError):
                continue  # header or junk
            z = float(row[2]) if len(row) > 2 and row[2].strip() else 0.0
            points.append((x, y, z))
    return points
