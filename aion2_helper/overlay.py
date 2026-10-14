"""Always-on-top overlay: a north-up radar plus cards for the Hidden Cubes in range.

Plain tkinter so there is nothing to install. Everything is drawn on one canvas
(rounded panel, animated radar, cards, toasts), redrawn ~30 times a second from
a snapshot taken a few times a second. On Windows the panel corners are
transparent and the window can be made click-through, so it never steals the
mouse from the game. Works over the game in borderless/windowed fullscreen
(exclusive fullscreen allows no window on top).

Mouse: drag to move, wheel over the radar to zoom, right click for the menu,
double click on the header for the compact (radar only) mode.
"""

from __future__ import annotations

import math
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from typing import Callable

from . import theme as T
from .detector import CubeDetector, Entity, EntityView, Snapshot
from .protocol import Vec3

W = 320
M = 12  # outer margin
HEADER_H = 66
RADAR = W - 2 * M
FRAME_MS = 33  # while something is happening (ping, toast, alert): smooth
IDLE_FRAME_MS = 66  # just the sweep: half the CPU
STATIC_FRAME_MS = 200  # with animations off
SNAPSHOT_S = 0.15
PING_S = 1.6
NEW_CUBE_S = 4.0
MAX_CUBE_ROWS = 4

# Backwards-compatible names used elsewhere.
compass = T.compass
cube_color = T.cube_color


# ───── sound ─────


def _play(notes: tuple[tuple[int, int], ...]) -> None:
    if sys.platform != "win32":
        return
    import winsound

    def go():
        for freq, ms in notes:
            winsound.Beep(freq, ms)

    threading.Thread(target=go, daemon=True).start()


CHIME_KEY = ((784, 70), (1047, 70), (1319, 120))  # rising: a red cube
CHIME_FREE = ((1047, 60), (1319, 60), (1568, 60), (2093, 140))  # brighter: a free one
CHIME_LEARNED = ((1319, 60), (1760, 140))


def beep(kind: str | None = "key") -> None:
    _play(CHIME_FREE if kind == "free" else CHIME_KEY)


# ───── overlay ─────


@dataclass
class _Ping:
    pos: Vec3
    t0: float
    color: str


@dataclass
class _Toast:
    text: str
    color: str
    t0: float
    dur: float


class Overlay:
    def __init__(self, detector: CubeDetector, on_close: Callable[[], None] | None = None):
        self.detector = detector
        self.config = detector.config
        self.on_close = on_close
        self.discovery = False
        self._actions: "queue.Queue[tuple]" = queue.Queue()
        self._alert_until = 0.0
        self._alert_color = T.KEY
        self._pings: list[_Ping] = []
        self._toasts: list[_Toast] = []
        self._visible = True
        self._snap: Snapshot | None = None
        self._snap_t = 0.0
        self._height = 0
        self._drag_from = (0, 0)
        self._radar_center = (W / 2, HEADER_H + RADAR / 2)

        ov = self.config["overlay"]
        self.compact = bool(ov.get("compact", False))
        self.animations = bool(ov.get("animations", True))

        self.root = tk.Tk()
        self.root.title("Aion2 Helper")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", ov.get("alpha", 0.9))
        bg = T.PANEL
        if T.is_windows():
            bg = T.CHROMA
            try:
                self.root.attributes("-transparentcolor", T.CHROMA)  # rounded corners
            except tk.TclError:
                bg = T.PANEL
        self.root.configure(bg=bg)
        self.root.geometry(f"{W}x400+{ov.get('x', 40)}+{ov.get('y', 200)}")
        self.fonts = T.pick_fonts(set(tkfont.families(self.root)))

        self.canvas = tk.Canvas(self.root, width=W, height=400, bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        c = self.canvas
        c.bind("<ButtonPress-1>", self._drag_start)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._drag_end)
        c.bind("<Double-Button-1>", self._double_click)
        c.bind("<Button-3>", self._menu_popup)
        c.bind("<MouseWheel>", lambda e: self._wheel(-1 if e.delta > 0 else 1))
        c.bind("<Button-4>", lambda e: self._wheel(-1))
        c.bind("<Button-5>", lambda e: self._wheel(1))
        self._build_menu()

        if ov.get("click_through"):
            self.root.after(100, lambda: self._set_click_through(True))
        self.root.after(50, self._tick)

    # ───── called from other threads ─────

    def post(self, action: str) -> None:
        """Queues a hotkey action; safe from any thread."""
        self._actions.put((action,))

    def alert(self, entity: Entity | None = None) -> None:
        self._actions.put(("alert", entity))

    def learned(self, code: int) -> None:
        self._actions.put(("learned", code))

    # ───── ui thread ─────

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        ov = self.config["overlay"]
        ov["x"], ov["y"] = self.root.winfo_x(), self.root.winfo_y()
        ov["compact"] = self.compact
        self.config.save()
        if self.on_close:
            self.on_close()
        self.root.destroy()

    def toast(self, text: str, color: str = T.ACCENT, seconds: float = 2.8) -> None:
        now = self.detector.clock()
        self._toasts = [t for t in self._toasts if now - t.t0 < t.dur][-2:]
        self._toasts.append(_Toast(text, color, now, seconds))

    def _handle(self, msg: tuple) -> None:
        action = msg[0]
        now = self.detector.clock()
        if action == "alert":
            entity: Entity | None = msg[1] if len(msg) > 1 else None
            kind = self.detector.cube_kind(entity.code) if entity else None
            color = T.cube_color(kind)
            self._alert_until = now + NEW_CUBE_S
            self._alert_color = color
            if entity is not None and entity.pos is not None:
                self._pings.append(_Ping(entity.pos, now, color))
            label = {"key": " con llave", "free": " gratis"}.get(kind, "")
            self.toast(f"◆  ¡Hidden Cube{label}!", color)
            if self.config["sound"]:
                beep(kind)
            else:
                self.root.bell()
        elif action == "learned":
            self.toast("Código del Hidden Cube aprendido solo", T.OK, 5)
            if self.config["sound"]:
                _play(CHIME_LEARNED)
        elif action == "mark":
            ok = self.detector.mark()
            self.toast("Marca guardada" if ok else "Sin registro: arranca con --log", T.ACCENT if ok else T.WARN)
        elif action == "tag_nearest":
            e = self.detector.tag_nearest()
            self.toast(f"Código {e.code} marcado como cubo" if e else "Nada cerca para marcar", T.OK if e else T.WARN)
        elif action == "click_through":
            ov = self.config["overlay"]
            ov["click_through"] = not ov.get("click_through")
            self._set_click_through(ov["click_through"])
            self.toast("Ratón atraviesa el overlay" if ov["click_through"] else "Overlay interactivo")
        elif action == "toggle_overlay":
            self._visible = not self._visible
            (self.root.deiconify if self._visible else self.root.withdraw)()
        elif action == "discovery":
            self.discovery = not self.discovery
            self.toast("Modo descubrimiento " + ("activado" if self.discovery else "desactivado"))
        elif action == "compact":
            self.compact = not self.compact
            self.config["overlay"]["compact"] = self.compact
        elif action == "reset_spots":
            n = self.detector.reset_spots()
            self.toast(f"{n} spots vuelven a estar pendientes", T.SPOT)
        elif action == "sound":
            self.config["sound"] = not self.config["sound"]
            self.toast("Sonido " + ("activado" if self.config["sound"] else "desactivado"))
        elif action == "quit":
            self.close()

    def _tick(self) -> None:
        try:
            while True:
                self._handle(self._actions.get_nowait())
        except queue.Empty:
            pass
        if not self.root.winfo_exists():
            return
        now = self.detector.clock()
        try:
            if self._snap is None or now - self._snap_t >= SNAPSHOT_S:
                self._snap = self.detector.snapshot()
                self._snap_t = now
            self._draw(self._snap, now)
        except Exception as exc:  # keep the overlay alive whatever happens
            self.canvas.delete("all")
            self.canvas.create_text(M, M, text=f"Error: {exc!r}", fill=T.BAD, anchor="nw", width=W - 2 * M)
        self.root.after(self._frame_ms(now), self._tick)

    def _frame_ms(self, now: float) -> int:
        if not self.animations:
            return STATIC_FRAME_MS
        busy = self._pings or self._toasts or now < self._alert_until
        return FRAME_MS if busy else IDLE_FRAME_MS

    # ───── drawing ─────

    def _anim(self, now: float) -> float:
        """Animation clock; frozen when animations are off."""
        return now if self.animations else 0.0

    def _draw(self, snap: Snapshot, now: float) -> None:
        c = self.canvas
        c.delete("all")
        self._panel_item = c.create_polygon(0, 0, 0, 0, fill=T.PANEL)  # sized at the end
        y = self._draw_header(snap, now)
        y = self._draw_radar(snap, now, y)
        if not self.compact:
            y = self._draw_next_spot(snap, y)
            y = self._draw_cubes(snap, now, y)
            if self.discovery:
                y = self._draw_discovery(snap, y)
            y = self._draw_footer(snap, y)
        else:
            y += 4
        self._draw_toasts(now)
        h = int(y + M / 2)
        edge = T.PANEL_EDGE
        if now < self._alert_until:
            edge = T.mix(T.PANEL_EDGE, self._alert_color, T.pulse(self._anim(now), 0.6, 0.3, 1.0))
        c.coords(self._panel_item, *T.rounded_rect(1, 1, W - 1, h - 1, 14))
        c.itemconfigure(self._panel_item, outline=edge, width=2 if now < self._alert_until else 1, smooth=True)
        c.tag_lower(self._panel_item)
        if h != self._height:
            self._height = h
            self.root.geometry(f"{W}x{h}")
            c.configure(height=h)

    def _chip(self, x: float, y: float, text: str, color: str, anchor: str = "w") -> float:
        """A small rounded label; returns its width."""
        c = self.canvas
        t = c.create_text(x + 7 if anchor == "w" else x - 7, y, text=text, fill=color, font=self.fonts["chip"], anchor=anchor)
        x0, y0, x1, y1 = c.bbox(t)
        r = c.create_polygon(
            *T.rounded_rect(x0 - 6, y0 - 2, x1 + 6, y1 + 2, 7),
            smooth=True, fill=T.fade(color, 0.16), outline=T.fade(color, 0.55),
        )  # fmt: skip
        c.tag_raise(t, r)
        return (x1 + 6) - (x0 - 6)

    def _draw_header(self, snap: Snapshot, now: float) -> float:
        c, f = self.canvas, self.fonts
        top = 10
        # logo: a cube seen from a corner
        lx, ly = M + 9, top + 14
        c.create_polygon(*T.diamond(lx, ly, 9), fill=T.fade(T.ACCENT, 0.25), outline="")
        c.create_polygon(*T.diamond(lx, ly, 6), fill=T.ACCENT, outline="")
        c.create_line(lx - 6, ly, lx, ly + 2, lx + 6, ly, fill=T.PANEL, width=1)
        c.create_line(lx, ly + 2, lx, ly + 6, fill=T.PANEL, width=1)
        c.create_text(M + 26, top + 7, text="HIDDEN CUBES", fill=T.TEXT, font=f["title"], anchor="w")

        x = M + 26 - 7
        if snap.map_name:
            x += self._chip(x, top + 24, snap.map_name.upper(), T.ACCENT) + 4
            rule = T.RULES.get(snap.cube_rule)
            if rule:
                self._chip(x, top + 24, rule[0], T.KEY if snap.cube_rule == "key" else T.FREE)
        elif self.detector.world is not None:
            self._chip(x, top + 24, "BUSCANDO MAPA…", T.MUTED)
        else:
            self._chip(x, top + 24, "SIN SPOTS", T.MUTED)

        n = len(snap.cubes)
        color = T.cube_color(snap.cubes[0].kind) if n else T.FAINT
        if n and now < self._alert_until:
            color = T.mix(color, "#ffffff", T.pulse(self._anim(now), 0.5, 0.0, 0.45))
        c.create_text(W - M - 2, top + 12, text=str(n), fill=color, font=f["big"], anchor="e")
        c.create_text(W - M - 2, top + 32, text="CERCA", fill=T.MUTED, font=f["tiny"], anchor="e")

        # status lights
        st = snap.status
        game = T.OK if st.startswith(("Conectado", "DEMO", "Reproducción")) else T.WARN if st.startswith("Buscando") else T.MUTED
        pos = T.OK if snap.self_learned else T.WARN
        spots = T.OK if snap.map_name else T.WARN if self.detector.world is not None else T.BAD
        learning = not self.config["cube_codes"] and n == 0
        lights = [("JUEGO", game), ("POSICIÓN", pos), ("SPOTS", spots)]
        if self.detector.world is not None:
            lights.append(("CÓDIGO", T.WARN if learning else T.OK))
        x = M + 2
        for label, col in lights:
            glow = T.pulse(self._anim(now), 1.8, 0.0, 0.35) if col == T.WARN else 0.0
            c.create_oval(x - 1, top + 45, x + 7, top + 53, fill=T.fade(col, 0.18 + glow), outline="")
            c.create_oval(x + 1, top + 47, x + 5, top + 51, fill=col, outline="")
            t = c.create_text(x + 10, top + 49, text=label, fill=T.MUTED, font=f["tiny"], anchor="w")
            x = c.bbox(t)[2] + 10
        return HEADER_H

    # radar

    def _screen_bearing(self, deg: float) -> float:
        b = -deg if self.config["radar_mirror"] else deg
        return (b + self.config["radar_rotation_deg"]) % 360

    def _draw_radar(self, snap: Snapshot, now: float, top: float) -> float:
        c, f = self.canvas, self.fonts
        t = self._anim(now)
        cx, cy = W / 2, top + RADAR / 2
        self._radar_center = (cx, cy)
        r = RADAR / 2 - 4
        rng = self.config["radar_range_m"]
        upm = self.config["units_per_meter"]

        # disc with a soft vignette and an outer halo
        c.create_oval(cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4, outline=T.fade(T.ACCENT, 0.12), width=3)
        for i, k in enumerate((1.0, 0.82, 0.62, 0.4)):
            col = T.mix(T.RADAR_BG, "#0e2638", i * 0.28)
            c.create_oval(cx - r * k, cy - r * k, cx + r * k, cy + r * k, fill=col, outline="")
        alerting = now < self._alert_until
        rim = T.mix(T.RING, self._alert_color, T.pulse(t, 0.6, 0.4, 1.0)) if alerting else T.RING
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=rim, width=2 if alerting else 1)
        for k in (1, 2):
            rr = r * k / 3
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=T.RING, dash=(2, 4))
        north = self._screen_bearing(0)
        for b in (north, north + 90):
            c.create_line(*T.polar(cx, cy, r, b), *T.polar(cx, cy, r, b + 180), fill=T.fade(T.RING, 0.6, T.RADAR_BG))
        for deg in range(0, 360, 10):
            b = north + deg
            ln = 7 if deg % 90 == 0 else 4 if deg % 30 == 0 else 2
            c.create_line(*T.polar(cx, cy, r, b), *T.polar(cx, cy, r - ln, b), fill=T.FAINT if deg % 30 else T.MUTED)
        for deg, label in T.CARDINALS:
            x, y = T.polar(cx, cy, r - 15, north + deg)
            c.create_text(x, y, text=label, fill=T.ACCENT if label == "N" else T.MUTED, font=f["chip"])
        for k in (1, 2, 3):
            x, y = T.polar(cx, cy, r * k / 3 - 2, 135)
            c.create_text(x, y, text=f"{rng * k / 3:.0f}", fill=T.FAINT, font=f["tiny"], anchor="se")

        # sweep with a fading trail
        sweep = (t * 72) % 360
        if self.animations:
            for i in range(12):
                col = T.fade(T.ACCENT, 0.2 * (1 - i / 12), T.RADAR_BG)
                c.create_arc(cx - r, cy - r, cx + r, cy + r, start=90 - sweep + i * 3, extent=3.2, fill=col, outline="", style="pieslice")
            c.create_line(cx, cy, *T.polar(cx, cy, r, sweep), fill=T.fade(T.ACCENT, 0.7, T.RADAR_BG), width=1)

        def lit(bearing: float) -> float:
            """How much the sweep just lit something at this bearing (0..1)."""
            if not self.animations:
                return 0.0
            d = T.angle_diff(bearing, sweep)
            return max(0.0, 1 - d / 60)

        me = snap.self_pos
        if me is None:
            c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=T.FAINT, outline="")
            c.create_text(cx, cy + 22, text="aprendiendo tu posición…", fill=T.MUTED, font=f["small"])
            c.create_text(cx, cy + 38, text="muévete un poco", fill=T.FAINT, font=f["tiny"])
            return top + RADAR

        def plot(p: Vec3) -> tuple[float, float, bool, float]:
            dist = math.hypot(p[0] - me[0], p[1] - me[1]) / upm
            b = self._screen_bearing(math.degrees(math.atan2(p[1] - me[1], p[0] - me[0])))
            clipped = dist > rng
            x, y = T.polar(cx, cy, r if clipped else r * dist / rng, b)
            return x, y, clipped, b

        # spots
        for s in snap.spots:
            if s.status == "cube":
                continue
            x, y, clipped, b = plot(s.pos)
            if clipped:
                continue
            if s.status == "empty":
                c.create_oval(x - 1.5, y - 1.5, x + 1.5, y + 1.5, fill=T.SPOT_DONE, outline="")
            else:
                col = T.mix(T.fade(T.SPOT, 0.7, T.RADAR_BG), "#cfe6ff", lit(b) * 0.6)
                c.create_oval(x - 3.5, y - 3.5, x + 3.5, y + 3.5, outline=col, width=1.5 if s.status == "learned" else 1)

        # the next spot to check: marching dashes towards it
        ns = snap.next_spot
        if ns is not None:
            x, y, clipped, b = plot(ns.pos)
            col = T.fade(T.ACCENT, 0.8, T.RADAR_BG)
            end = T.polar(cx, cy, r - 10, b) if clipped else (x, y)
            c.create_line(cx, cy, *end, fill=T.fade(T.ACCENT, 0.45, T.RADAR_BG), dash=(4, 4), dashoffset=-int(t * 24) % 8)
            if clipped:
                c.create_polygon(*T.arrow(*T.polar(cx, cy, r - 7, b), b, 7), fill=col, outline="")
            else:
                pr = 6 + 3 * T.pulse(t, 1.2)
                c.create_oval(x - pr, y - pr, x + pr, y + pr, outline=col, width=2)
                c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=col, outline="")

        if self.discovery:
            for v in snap.nearby:
                if v.entity.pos is not None:
                    x, y, clipped, _ = plot(v.entity.pos)
                    if not clipped:
                        c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=T.FAINT, outline="")

        # you
        c.create_oval(cx - 9, cy - 9, cx + 9, cy + 9, fill=T.fade(T.ACCENT, 0.12, T.RADAR_BG), outline="")
        c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=T.fade(T.ACCENT, 0.35, T.RADAR_BG), outline="")
        c.create_oval(cx - 2.5, cy - 2.5, cx + 2.5, cy + 2.5, fill=T.TEXT, outline="")

        # "ping" waves where a cube just appeared
        self._pings = [p for p in self._pings if now - p.t0 < PING_S]
        for p in self._pings:
            x, y, clipped, b = plot(p.pos)
            if clipped:
                x, y = T.polar(cx, cy, r - 8, b)
            a = (now - p.t0) / PING_S
            for k in range(3):
                e = T.ease_out(a * 1.3 - k * 0.2)
                if e <= 0:
                    continue
                pr = 6 + 42 * e
                c.create_oval(x - pr, y - pr, x + pr, y + pr, outline=T.fade(p.color, (1 - a) * 0.9, T.RADAR_BG), width=2)

        # cubes, nearest drawn last (on top)
        for v in reversed(snap.cubes):
            if v.entity.pos is None:
                continue
            col = T.cube_color(v.kind)
            x, y, clipped, b = plot(v.entity.pos)
            glow = max(lit(b), T.pulse(t, 1.1, 0.0, 0.5))
            if clipped:
                ax, ay = T.polar(cx, cy, r - 8, b)
                c.create_polygon(*T.arrow(ax, ay, b, 9), fill=col, outline=T.mix(col, "#ffffff", 0.4))
                tx, ty = T.polar(cx, cy, r - 26, b)
                c.create_text(tx, ty, text=T.fmt_distance(v.distance_m), fill=col, font=f["chip"])
                continue
            for hr, a in ((17, 0.10), (12, 0.2 + 0.15 * glow)):
                c.create_oval(x - hr, y - hr, x + hr, y + hr, fill=T.fade(col, a, T.RADAR_BG), outline="")
            s = 6 + 1.5 * glow
            c.create_polygon(*T.diamond(x, y, s), fill=col, outline=T.mix(col, "#ffffff", 0.55))
            c.create_line(x - s, y, x, y + s * 0.35, x + s, y, fill=T.mix(col, "#000000", 0.35))
            label = T.fmt_distance(v.distance_m)
            hint = self._height_hint(v, me)
            if hint:
                label += f" {hint}"
            below = y + 18 < cy + r * 0.8  # near the bottom rim the label goes above
            tid = c.create_text(x, y + 18 if below else y - 18, text=label, fill=T.TEXT, font=f["chip"])
            x0, y0, x1, y1 = c.bbox(tid)
            pill = c.create_polygon(*T.rounded_rect(x0 - 4, y0 - 1, x1 + 4, y1 + 1, 6), smooth=True,
                                    fill=T.fade(col, 0.28, T.RADAR_BG), outline=T.fade(col, 0.7, T.RADAR_BG))  # fmt: skip
            c.tag_raise(tid, pill)
        return top + RADAR

    def _height_hint(self, v: EntityView, me: Vec3 | None) -> str:
        if me is None or v.entity.pos is None:
            return ""
        return T.height_hint((v.entity.pos[2] - me[2]) / self.config["units_per_meter"])

    # cards

    def _card(self, y0: float, y1: float, accent: str | None = None, highlight: float = 0.0) -> None:
        bg = T.mix(T.PANEL_HI, accent, highlight * 0.25) if accent else T.PANEL_HI
        outline = T.fade(accent, 0.35 + 0.4 * highlight) if accent else T.PANEL_EDGE
        self.canvas.create_polygon(*T.rounded_rect(M, y0, W - M, y1, 9), smooth=True, fill=bg, outline=outline)

    def _draw_next_spot(self, snap: Snapshot, y: float) -> float:
        ns = snap.next_spot
        if ns is None or ns.distance_m is None:
            return y + 6
        c, f = self.canvas, self.fonts
        y0, y1 = y + 8, y + 52
        self._card(y0, y1, T.ACCENT)
        b = self._screen_bearing(ns.bearing_deg)
        ax, ay = M + 22, (y0 + y1) / 2
        c.create_oval(ax - 13, ay - 13, ax + 13, ay + 13, fill=T.fade(T.ACCENT, 0.15, T.PANEL_HI), outline="")
        c.create_polygon(*T.arrow(ax, ay, b, 9), fill=T.ACCENT, outline="")
        c.create_text(M + 42, y0 + 12, text="SIGUIENTE SPOT", fill=T.MUTED, font=f["tiny"], anchor="w")
        c.create_text(M + 42, y0 + 29, text=f"{T.fmt_distance(ns.distance_m)} · {T.compass(b)}", fill=T.TEXT, font=f["value"], anchor="w")

        rng = self.config["radar_range_m"]
        near = [s for s in snap.spots if s.distance_m is not None and s.distance_m <= rng]
        done = sum(1 for s in near if s.status == "empty")
        total = sum(1 for s in near if s.status in ("empty", "pending"))
        bx1, bx0 = W - M - 12, W - M - 100
        c.create_text(bx1, y0 + 13, text=f"{snap.pending_near} pendientes", fill=T.TEXT, font=f["small"], anchor="e")
        c.create_polygon(*T.rounded_rect(bx0, y0 + 25, bx1, y0 + 31, 3), smooth=True, fill=T.SPOT_DONE, outline="")
        if total and done:
            c.create_polygon(*T.rounded_rect(bx0, y0 + 25, bx0 + (bx1 - bx0) * done / total, y0 + 31, 3),
                             smooth=True, fill=T.OK, outline="")  # fmt: skip
        c.create_text(bx1, y0 + 38, text=f"{done}/{total} revisados en {rng} m", fill=T.FAINT, font=f["tiny"], anchor="e")
        return y1

    def _draw_cubes(self, snap: Snapshot, now: float, y: float) -> float:
        c, f = self.canvas, self.fonts
        if not snap.cubes:
            msg, sub = self._empty_state(snap)
            y0, y1 = y + 8, y + (46 if sub else 32)
            self._card(y0, y1)
            c.create_text(W / 2, y0 + 14, text=msg, fill=T.MUTED, font=f["body"])
            if sub:
                c.create_text(W / 2, y0 + 30, text=sub, fill=T.FAINT, font=f["tiny"])
            return y1
        for i, v in enumerate(snap.cubes[:MAX_CUBE_ROWS]):
            col = T.cube_color(v.kind)
            y0, y1 = y + 8, y + 42
            fresh = max(0.0, 1 - v.age_s / NEW_CUBE_S)
            self._card(y0, y1, col, fresh * T.pulse(self._anim(now), 0.5, 0.5, 1.0) if fresh else (0.25 if i == 0 else 0.0))
            yc = (y0 + y1) / 2
            c.create_polygon(*T.rounded_rect(M + 1, y0 + 6, M + 4, y1 - 6, 1.5), smooth=True, fill=col, outline="")
            ax = M + 22
            if v.bearing_deg is not None:
                b = self._screen_bearing(v.bearing_deg)
                c.create_oval(ax - 11, yc - 11, ax + 11, yc + 11, fill=T.fade(col, 0.15, T.PANEL_HI), outline="")
                c.create_polygon(*T.arrow(ax, yc, b, 8), fill=col, outline="")
                where = T.compass(b)
            else:
                c.create_polygon(*T.diamond(ax, yc, 6), fill=col, outline="")
                where = ""
            t = c.create_text(M + 40, yc, text=T.fmt_distance(v.distance_m) if v.distance_m is not None else "distancia ?",
                              fill=T.TEXT, font=f["value"], anchor="w")  # fmt: skip
            x = c.bbox(t)[2] + 6
            if where:
                t = c.create_text(x, yc + 1, text=where, fill=T.MUTED, font=f["small"], anchor="w")
                x = c.bbox(t)[2] + 6
            hint = self._height_hint(v, snap.self_pos)
            if hint:
                c.create_text(x, yc + 1, text=hint + " m", fill=T.ACCENT if hint.startswith("▲") else T.SPOT, font=f["small"], anchor="w")
            right = W - M - 10
            chip = T.KIND_CHIP.get(v.kind)
            if chip:
                right -= self._chip(right, yc, chip, col, anchor="e") + 6
            c.create_text(right, yc, text=T.fmt_age(v.age_s), fill=T.FAINT, font=f["tiny"], anchor="e")
            y = y1 - 4
        extra = len(snap.cubes) - MAX_CUBE_ROWS
        if extra > 0:
            c.create_text(W / 2, y + 14, text=f"+{extra} más", fill=T.MUTED, font=f["small"])
            y += 20
        return y + 4

    def _empty_state(self, snap: Snapshot) -> tuple[str, str]:
        if self.detector.world is None and not self.config["cube_codes"]:
            return "Sin calibrar", "ejecuta 'update-spots' o pulsa F9 junto a un cubo"
        if not self.config["cube_codes"]:
            return "Aprendiendo el código del cubo…", "se aprende solo al ver el primero"
        if snap.next_spot is not None:
            return "Ningún cubo a la vista", "sigue la flecha azul al siguiente spot"
        return "Ningún cubo a la vista", ""

    def _draw_discovery(self, snap: Snapshot, y: float) -> float:
        c, f = self.canvas, self.fonts
        c.create_text(M + 2, y + 14, text="ENTIDADES CERCANAS · F9 = ES UN CUBO", fill=T.MUTED, font=f["tiny"], anchor="w")
        y += 22
        if not snap.nearby:
            c.create_text(M + 2, y + 6, text="(ninguna entidad sin clasificar cerca)", fill=T.FAINT, font=f["tiny"], anchor="w")
            y += 14
        for v in snap.nearby:
            d = f"{v.distance_m:>4.0f}m {T.compass(self._screen_bearing(v.bearing_deg)):<2}" if v.distance_m is not None else "   ?    "
            c.create_text(M + 2, y + 6, text=f"{v.entity.code:>8}  k{v.entity.kind:02X}  {d}  {T.fmt_age(v.age_s)}",
                          fill=T.MUTED, font=f["mono"], anchor="w")  # fmt: skip
            y += 14
        return y

    def _draw_footer(self, snap: Snapshot, y: float) -> float:
        c, f = self.canvas, self.fonts
        y += 8
        c.create_line(M + 4, y, W - M - 4, y, fill=T.PANEL_EDGE)
        status = snap.status
        if snap.world and not snap.map_name:
            status += " · " + snap.world
        c.create_text(M + 2, y + 10, text=status, fill=T.MUTED, font=f["tiny"], anchor="w", width=W - 2 * M)
        c.create_text(M + 2, y + 24, text="rueda: zoom · clic dcho: menú · doble clic: compacto",
                      fill=T.FAINT, font=f["tiny"], anchor="w")  # fmt: skip
        return y + 32

    def _draw_toasts(self, now: float) -> None:
        c, f = self.canvas, self.fonts
        self._toasts = [t for t in self._toasts if now - t.t0 < t.dur]
        cx, cy = self._radar_center
        base = cy + RADAR / 2 - 26
        for i, t in enumerate(reversed(self._toasts)):
            age = now - t.t0
            a = min(1.0, age / 0.2, (t.dur - age) / 0.4)
            slide = (1 - T.ease_out(age / 0.25)) * 10
            y = base - i * 26 + slide
            tid = c.create_text(cx, y, text=t.text, fill=T.mix(T.PANEL, T.TEXT, a), font=f["body"])
            x0, y0, x1, y1 = c.bbox(tid)
            pill = c.create_polygon(*T.rounded_rect(x0 - 10, y0 - 4, x1 + 10, y1 + 4, 10), smooth=True,
                                    fill=T.mix(T.PANEL, T.mix(T.PANEL_HI, t.color, 0.18), a),
                                    outline=T.mix(T.PANEL, t.color, a * 0.8))  # fmt: skip
            c.tag_raise(tid, pill)

    # ───── mouse ─────

    def _drag_start(self, e) -> None:
        self._drag_from = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._drag_from[0]}+{e.y_root - self._drag_from[1]}")

    def _drag_end(self, e) -> None:
        ov = self.config["overlay"]
        ov["x"], ov["y"] = self.root.winfo_x(), self.root.winfo_y()

    def _double_click(self, e) -> None:
        if e.y <= HEADER_H:
            self._handle(("compact",))

    def _wheel(self, steps: int) -> None:
        rng = T.zoom(self.config["radar_range_m"], steps)
        if rng != self.config["radar_range_m"]:
            self.config["radar_range_m"] = rng
            self._snap = None  # spots in range change with the zoom
            self.toast(f"Alcance del radar: {rng} m", T.ACCENT, 1.2)

    def _build_menu(self) -> None:
        m = tk.Menu(self.root, tearoff=0, bg=T.PANEL_HI, fg=T.TEXT, activebackground=T.ACCENT,
                    activeforeground=T.PANEL, bd=0, relief="flat", font=self.fonts["body"])  # fmt: skip
        self._vars = {k: tk.BooleanVar(self.root) for k in ("compact", "sound", "click", "discovery", "anim")}
        m.add_checkbutton(label="Modo compacto", variable=self._vars["compact"], command=lambda: self._handle(("compact",)))
        m.add_checkbutton(label="Sonido", variable=self._vars["sound"], command=lambda: self._handle(("sound",)))
        m.add_checkbutton(label="Animaciones", variable=self._vars["anim"], command=self._toggle_animations)
        m.add_checkbutton(label="Modo descubrimiento (F7)", variable=self._vars["discovery"], command=lambda: self._handle(("discovery",)))
        m.add_checkbutton(label="El ratón atraviesa (F10)", variable=self._vars["click"], command=lambda: self._handle(("click_through",)))
        m.add_separator()
        m.add_command(label="Spots pendientes otra vez (F6)", command=lambda: self._handle(("reset_spots",)))
        m.add_command(label="Ocultar (F11)", command=lambda: self._handle(("toggle_overlay",)))
        m.add_separator()
        m.add_command(label="Cerrar", command=self.close)
        self.menu = m

    def _menu_popup(self, e) -> None:
        self._vars["compact"].set(self.compact)
        self._vars["sound"].set(bool(self.config["sound"]))
        self._vars["click"].set(bool(self.config["overlay"].get("click_through")))
        self._vars["discovery"].set(self.discovery)
        self._vars["anim"].set(self.animations)
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_animations(self) -> None:
        self.animations = not self.animations
        self.config["overlay"]["animations"] = self.animations

    # ───── window plumbing ─────

    def _set_click_through(self, on: bool) -> None:
        if sys.platform != "win32":
            return
        import ctypes

        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_NOACTIVATE
        style = style | WS_EX_TRANSPARENT if on else style & ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def start_hotkeys(overlay: Overlay, hotkeys: dict[str, str]):
    """Global hotkeys (they work while the game has focus). Returns the listener, or None."""
    try:
        from pynput import keyboard
    except ImportError:
        return None
    mapping = {combo: (lambda a=action: overlay.post(a)) for action, combo in hotkeys.items() if combo}
    try:
        listener = keyboard.GlobalHotKeys(mapping)
        listener.daemon = True
        listener.start()
        return listener
    except Exception:
        return None
