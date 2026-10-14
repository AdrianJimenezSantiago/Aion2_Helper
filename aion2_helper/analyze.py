"""Finds the Hidden Cube NPC code from a discovery log.

Run the overlay with ``--log`` and press the mark hotkey (F8) every time you
stand next to a cube (you hear its chime or see it). Codes that keep showing up
right next to you at those marks, and rarely anywhere else, are the cube.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

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


def main(path: Path, top: int = 10) -> None:
    records = load(path)
    marks = sum(1 for r in records if r["type"] == "mark")
    print(f"{path}: {len(records)} registros, {marks} marcas")
    if not marks:
        print("No hay marcas. Pulsa F8 junto a cada cubo mientras grabas con --log.")
        return
    print(f"{'código':>10} {'puntuación':>10} {'marcas':>7} {'spawns':>7}")
    for code, score, h, n in rank(records)[:top]:
        print(f"{code:>10} {score:>10.2f} {h:>7} {n:>7}")
    print("\nAñade el mejor código a \"cube_codes\" en config.json (o pulsa F9 junto al cubo).")
