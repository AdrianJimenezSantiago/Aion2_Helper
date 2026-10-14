"""Finds the Hidden Cube NPC code from a discovery log.

With the community spots downloaded (``update-spots``) no marks are needed:
codes whose entities sit exactly on known cube spots are the cube.

Without them, run the overlay with ``--log`` and press the mark hotkey (F8)
every time you stand next to a cube (you hear its chime or see it). Codes that
keep showing up right next to you at those marks, and rarely anywhere else,
are the cube.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from .worldmap import CUBE_SPOT_RADIUS, CubeCodeLearner, WorldData, WorldLocator, to_data

NEAR_UNITS = 1500.0  # 15 m: a mark this close to an entity counts for it
WINDOW_S = 600.0  # an entity spawned this long before a mark may still be there
RECENT_S = 120.0  # without positions, only fairly recent spawns count


def load(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def rank(records: list[dict]) -> list[tuple[int, float, int, int]]:
    """Returns (code, score, marks hit, total spawns) best first."""
    total = Counter(r["code"] for r in records if r["type"] == "spawn" and r.get("code") is not None)
    spawns = [r for r in records if r["type"] == "spawn" and r.get("code") is not None]
    hits: dict[int, int] = defaultdict(int)
    for m in (r for r in records if r["type"] == "mark"):
        t, me = m["t"], m.get("self")
        seen: set[int] = set()
        for s in spawns:
            if not (0 <= t - s["t"] <= WINDOW_S):
                continue
            if me and s.get("pos"):
                if math.dist(me[:2], s["pos"][:2]) <= NEAR_UNITS:
                    seen.add(s["code"])
            elif t - s["t"] <= RECENT_S:
                seen.add(s["code"])
        for c in seen:
            hits[c] += 1
    out = [(c, h / (1 + math.log(total[c])), h, total[c]) for c, h in hits.items()]
    return sorted(out, key=lambda x: -x[1])


def rank_by_spots(records: list[dict], world: WorldData) -> tuple[list[tuple[int, int, int, float]], list[str]]:
    """Returns ((code, spots hit, entities, precision) best first, maps located)."""
    locator = WorldLocator(world)
    learner = CubeCodeLearner()
    maps: list[str] = []
    pending: list[dict] = []

    def learn(r: dict) -> None:
        m = locator.map
        p = tuple(r["pos"])
        spot = m.cubes.nearest(to_data(locator.transform, p), CUBE_SPOT_RADIUS, locator.height)
        learner.observe(r["code"], r["id"], (m.map_id, spot) if spot is not None else None)

    for r in records:
        if r["type"] == "zone":
            locator.reset()
            pending.clear()
        elif r["type"] == "spawn" and r.get("pos") and r.get("code") is not None:
            if locator.map is None:
                pending.append(r)
                if locator.observe(tuple(r["pos"])):
                    maps.append(locator.map.name)
                    for q in pending:
                        learn(q)
                    pending.clear()
            else:
                learn(r)
    out = [
        (code, len(s.spots), len(s.entities), learner.precision(code))
        for code, s in learner.stats.items()
        if s.spots
    ]
    return sorted(out, key=lambda x: (-x[1] * x[3], -x[1])), maps


def main(path: Path, top: int = 10, world: WorldData | None = None) -> None:
    records = load(path)
    marks = sum(1 for r in records if r["type"] == "mark")
    print(f"{path}: {len(records)} registros, {marks} marcas")
    if world is not None:
        ranked, maps = rank_by_spots(records, world)
        print(f"Mapas reconocidos: {', '.join(maps) or 'ninguno'}")
        if ranked:
            print(f"{'código':>10} {'spots':>6} {'entidades':>9} {'precisión':>9}")
            for code, hits, n, prec in ranked[:top]:
                print(f"{code:>10} {hits:>6} {n:>9} {prec:>9.0%}")
            print("\nUn código con 2 o más spots y precisión alta es un Hidden Cube.\n")
    if not marks:
        if world is None:
            print("No hay marcas ni spots de la comunidad. Ejecuta 'update-spots', o pulsa F8 junto a")
            print("cada cubo mientras grabas con --log.")
        return
    print(f"{'código':>10} {'puntuación':>10} {'marcas':>7} {'spawns':>7}")
    for code, score, h, n in rank(records)[:top]:
        print(f"{code:>10} {score:>10.2f} {h:>7} {n:>7}")
    print("\nAñade el mejor código a \"cube_codes\" en config.json (o pulsa F9 junto al cubo).")
