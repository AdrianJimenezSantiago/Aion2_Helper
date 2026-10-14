"""Look of the overlay: palette, fonts and the pure geometry the drawing code needs.

Tk canvases have no per-item transparency, so glows, fades and trails are faked
by blending a colour towards the background (``mix``). Everything here is pure
and testable; ``overlay.py`` does the drawing.
"""

from __future__ import annotations

import math
import sys

# ───── palette ─────

# Window background made transparent on Windows (rounded panel corners).
CHROMA = "#010203"

PANEL = "#0c1118"
PANEL_HI = "#131b25"
PANEL_EDGE = "#223041"
TEXT = "#e8eef5"
MUTED = "#8a96a6"
FAINT = "#4b5767"

ACCENT = "#56c7ff"  # the tool's own colour: radar, sweep, selection
RADAR_BG = "#08131d"
RING = "#16324a"

KEY = "#ff4d5e"  # cube that needs a key (the red ones)
FREE = "#ffc94d"  # cube that opens free (the gold ones)
UNKNOWN = "#ff9f43"
SPOT = "#4d9fff"
SPOT_DONE = "#2a3644"

OK = "#3ddc84"
WARN = "#ffb84d"
BAD = "#ff5c5c"

COMPASS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
CARDINALS = [(0, "N"), (90, "E"), (180, "S"), (270, "O")]

RULES = {
    "key": ("LLAVE", "Todos los cubos de este mapa necesitan llave"),
    "mixed": ("GRATIS / LLAVE", "Cubos gratis, o con llave para mejor botín"),
}
KIND_CHIP = {"key": "LLAVE", "free": "GRATIS"}

# Radar ranges the mouse wheel steps through (metres).
ZOOM_STEPS = [30, 45, 60, 90, 120, 180, 240]  # thirds stay round on the ring labels


def cube_color(kind: str | None) -> str:
    return {"key": KEY, "free": FREE}.get(kind, UNKNOWN)


# ───── colour maths ─────


def _rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(a: str, b: str, t: float) -> str:
    """Colour ``t`` of the way from a to b (t clamped to 0..1)."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return "#%02x%02x%02x" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t), round(ba + (bb - ba) * t))


def fade(color: str, alpha: float, bg: str = PANEL) -> str:
    """``color`` drawn at ``alpha`` opacity over ``bg``."""
    return mix(bg, color, alpha)


def pulse(t: float, period: float = 1.4, lo: float = 0.0, hi: float = 1.0) -> float:
    """Smooth 0..1 breathing value."""
    return lo + (hi - lo) * (0.5 - 0.5 * math.cos(2 * math.pi * t / period))


def ease_out(x: float) -> float:
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return 1 - (1 - x) ** 3


# ───── geometry ─────


def compass(deg: float) -> str:
    return COMPASS[int((deg % 360 + 22.5) // 45) % 8]


def polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    """Screen point at bearing ``deg`` (0 = up, clockwise) and radius r."""
    a = math.radians(deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def rounded_rect(x0: float, y0: float, x1: float, y1: float, r: float) -> list[float]:
    """Polygon points for a rounded rectangle (draw with ``smooth=True``)."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]  # fmt: skip


def arrow(cx: float, cy: float, deg: float, size: float) -> list[float]:
    """A dart pointing at bearing ``deg``, centred on (cx, cy)."""
    tip = polar(cx, cy, size, deg)
    left = polar(cx, cy, size * 0.75, deg + 140)
    notch = polar(cx, cy, size * 0.25, deg + 180)
    right = polar(cx, cy, size * 0.75, deg - 140)
    return [*tip, *left, *notch, *right]


def diamond(cx: float, cy: float, s: float) -> list[float]:
    return [cx, cy - s, cx + s, cy, cx, cy + s, cx - s, cy]


def angle_diff(a: float, b: float) -> float:
    """How far behind ``b`` the angle ``a`` is, in 0..360 (clockwise)."""
    return (b - a) % 360


def fmt_distance(m: float | None) -> str:
    if m is None:
        return "?"
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{m:.0f} m"


def fmt_age(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    return f"{s // 3600}h"


def height_hint(dz_m: float, threshold: float = 4.0) -> str:
    """▲/▼ when the cube is clearly above (a cliff) or below (water, a cave) you."""
    if dz_m >= threshold:
        return f"▲{dz_m:.0f}"
    if dz_m <= -threshold:
        return f"▼{-dz_m:.0f}"
    return ""


def zoom(current: float, steps: int) -> int:
    """Next radar range from the wheel (steps > 0 zooms out)."""
    i = min(range(len(ZOOM_STEPS)), key=lambda k: abs(ZOOM_STEPS[k] - current))
    return ZOOM_STEPS[max(0, min(len(ZOOM_STEPS) - 1, i + steps))]


# ───── fonts ─────


def pick_fonts(families: set[str]) -> dict[str, tuple]:
    """Fonts for each text role, falling back to what the system has."""

    def first(*names: str) -> str:
        return next((n for n in names if n in families), "TkDefaultFont")

    ui = first("Segoe UI", "Inter", "Noto Sans", "DejaVu Sans", "Helvetica", "Arial")
    bold = first("Segoe UI Semibold", "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans", "Helvetica")
    mono = first("Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", "Courier New")
    w = "bold" if bold == ui else "normal"
    return {
        "title": (bold, 10, "bold"),
        "big": (bold, 20, "bold"),
        "value": (bold, 11, "bold" if w == "bold" else "normal"),
        "body": (ui, 9),
        "small": (ui, 8),
        "tiny": (ui, 7),
        "chip": (bold, 7, "bold"),
        "mono": (mono, 8),
    }


def is_windows() -> bool:
    return sys.platform == "win32"
