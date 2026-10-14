"""A made-up session to see the overlay without the game: you walk, cubes appear and vanish."""

from __future__ import annotations

import math
import random
import threading
import time

from .detector import CubeDetector
from .protocol import Despawn, Spawn

DEMO_CUBE_CODE = 9_990_001


def run(detector: CubeDetector, stop: threading.Event) -> None:
    if DEMO_CUBE_CODE not in detector.config["cube_codes"]:
        detector.config["cube_codes"] = list(detector.config["cube_codes"]) + [DEMO_CUBE_CODE]
    tracker = detector.tracker
    tracker.fixed_key = (b"\x00\x00", 0)  # pretend it was learned
    detector.status = "DEMO (datos inventados)"
    origin = (74914.0, -84788.0, 8853.0)
    next_id = 50_000
    alive: list[int] = []
    t0 = time.time()
    while not stop.is_set():
        t = time.time() - t0
        me = (origin[0] + 2500 * math.cos(t / 20), origin[1] + 2500 * math.sin(t / 20), origin[2])
        tracker.position = me
        if random.random() < 0.08:
            a, d = random.uniform(0, 2 * math.pi), random.uniform(500, 5000)
            pos = (me[0] + d * math.cos(a), me[1] + d * math.sin(a), me[2])
            code = DEMO_CUBE_CODE if random.random() < 0.3 else random.choice([2920548, 2000002, 2000004])
            detector.handle(Spawn(next_id, 0x0C, code, pos))
            alive.append(next_id)
            next_id += 1
        if alive and random.random() < 0.04:
            detector.handle(Despawn(alive.pop(0), 7))
        stop.wait(0.5)
