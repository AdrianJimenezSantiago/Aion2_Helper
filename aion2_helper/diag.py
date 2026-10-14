"""Diagnostics: what the capture sees at each stage, so "it does not work" can say why.

Every stage counts what it gets: TCP segments, frames, LZ4 bundles, opcodes,
spawn records, positions, the map vote, the self-position search. ``report``
turns that into a few lines that go to the overlay's diagnostics panel, to
``logs/aion2helper.log`` every 30 s, and to ``python -m aion2_helper diagnose``.

The report holds no character names or chat: only counts, opcodes, NPC codes
and rounded positions, so it can be shared.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import Counter, deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from .protocol import Vec3

if TYPE_CHECKING:
    from .detector import CubeDetector

log = logging.getLogger("aion2helper")


class Diagnostics:
    def __init__(self):
        self._lock = threading.Lock()
        self.started = time.time()
        self.counts: Counter[str] = Counter()
        self.opcodes: Counter[str] = Counter()
        self.recent_spawns: deque[tuple[int | None, Vec3]] = deque(maxlen=300)
        self.zone_times: deque[float] = deque(maxlen=50)

    def count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counts[key] += n

    def opcode(self, op: bytes) -> None:
        with self._lock:
            self.opcodes[op.hex(" ")] += 1

    def spawn(self, code: int | None, pos: Vec3 | None) -> None:
        with self._lock:
            self.counts["spawns"] += 1
            if pos is not None:
                self.counts["spawns_with_pos"] += 1
                self.recent_spawns.append((code, pos))

    def zone(self, t: float) -> None:
        with self._lock:
            self.counts["zone_changes"] += 1
            self.zone_times.append(t)

    # ───── report ─────

    def report(self, detector: "CubeDetector | None" = None) -> list[str]:
        with self._lock:
            c = Counter(self.counts)
            ops = self.opcodes.most_common(8)
            spawns = list(self.recent_spawns)
            zones = list(self.zone_times)
        mins = max((time.time() - self.started) / 60, 1 / 60)
        lines = [
            f"Red: {c['seg_down']} segmentos del servidor, {c['seg_up']} del cliente, "
            f"{c['seg_other']} de otras conexiones del servidor (ignorados), {c['conn_locks']} conexiones fijadas",
            f"Tramas: {c['frames']} paquetes, {c['bundles']} bloques LZ4 ({c['bundle_fail']} fallidos), "
            f"{c['resync']} bytes de resincronización",
            "Opcodes más frecuentes: " + (", ".join(f"{op}×{n}" for op, n in ops) or "ninguno"),
            f"Spawns: {c['spawns']} ({c['spawns_with_pos']} con posición), {c['despawns']} bajas, "
            f"cambios de zona: {c['zone_changes']} ({c['zone_changes'] / mins:.1f}/min)",
        ]
        if zones and len(zones) >= 2:
            gaps = [b - a for a, b in zip(zones, zones[1:])]
            lines.append(f"  entre cambios de zona: mediana {statistics.median(gaps):.0f} s")
        if spawns:
            code, p = spawns[-1]
            lines.append(f"Último spawn: código {code} en ({p[0]:.0f}, {p[1]:.0f}, {p[2]:.0f})")
        if detector is not None:
            lines += self._detector_lines(detector, spawns)
        return lines

    def _detector_lines(self, det: "CubeDetector", spawns: list[tuple[int | None, Vec3]]) -> list[str]:
        out = [f"Estado: {det.status} · entidades vivas: {det.entity_count}"]
        tr = det.tracker
        if tr.locked:
            op, off = tr.fixed_key
            out.append(f"Posición propia: aprendida (opcode {op.hex(' ')}, offset {off}) = {_fmt(tr.position)}")
        else:
            best = tr.best_candidates(3)
            txt = "; ".join(f"{op.hex(' ')}@{off}: {c.samples} muestras, {c.good} suaves, {c.near_anchor} cerca" for (op, off), c in best)
            out.append(f"Posición propia: buscando ({len(tr._candidates)} candidatos) {txt}")
        loc = det.locator
        if loc is None:
            out.append("Spots: sin datos de la comunidad (ejecuta update-spots)")
            return out
        m = loc.map
        out.append(
            f"Mapa: {m.name if m else 'sin reconocer'} · ejes {loc.transform or '?'} · "
            f"altura {'sí' if loc.height else 'no'} · {loc.observed} posiciones vistas"
        )
        out.append("  votos: " + (", ".join(f"{mid}/{tf}={n}" for (mid, tf), n in loc.top_scores(4)) or "ninguno"))
        if spawns:
            out += _alignment(det, [p for _, p in spawns[-120:]])
        out.append(f"Códigos de cubo: {sorted(det.cube_codes) or 'aún ninguno'} · candidatos: {len(det.learner.stats)}")
        return out


def _fmt(p: Vec3 | None) -> str:
    return "?" if p is None else f"({p[0]:.0f}, {p[1]:.0f}, {p[2]:.0f})"


def _alignment(det: "CubeDetector", positions: list[Vec3]) -> list[str]:
    """How close recent spawns sit to each map's fixed points: tells a wrong map from wrong coordinates."""
    from .worldmap import MATCH_HEIGHT, MATCH_RADIUS, to_data

    tf = det.locator.transform or "xy"
    rows = []
    for map_id, m in det.world.maps.items():
        if not len(m.anchors):
            continue
        dists, hits = [], 0
        for p in positions:
            q = to_data(tf, p)
            i = m.anchors.nearest(q, 20000.0)
            if i is None:
                continue
            a = m.anchors.points[i]
            d = ((a[0] - q[0]) ** 2 + (a[1] - q[1]) ** 2) ** 0.5
            dists.append(d)
            if d <= MATCH_RADIUS and abs(a[2] - q[2]) <= MATCH_HEIGHT:
                hits += 1
        if dists:
            rows.append((hits, map_id, statistics.median(dists) / 100, len(dists)))
    rows.sort(reverse=True)
    out = [f"  alineación ({tf}, {len(positions)} spawns recientes):"]
    for hits, map_id, med, n in rows[:3]:
        out.append(f"    {map_id}: {hits} sobre un punto fijo, {n} a <200 m de alguno, mediana {med:.1f} m")
    if not rows:
        out.append("    ningún spawn a menos de 200 m de un punto de ningún mapa: coordenadas distintas")
    return out


# ───── log file ─────


def setup_logging(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "aion2helper.log"
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return path


def start_periodic_report(diag: Diagnostics, detector: "CubeDetector", stop: threading.Event, every_s: float = 30.0) -> None:
    def run():
        while not stop.wait(every_s):
            try:
                log.info("Diagnóstico\n  " + "\n  ".join(diag.report(detector)))
            except Exception:
                log.exception("no se pudo generar el diagnóstico")

    threading.Thread(target=run, name="aion2-diag", daemon=True).start()
