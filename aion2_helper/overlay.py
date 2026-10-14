"""Always-on-top overlay: a north-up radar plus the list of Hidden Cubes in range.

Plain tkinter so there is nothing to install. On Windows the window can be made
click-through, so it never steals the mouse from the game. Works over the game
in borderless/windowed fullscreen (exclusive fullscreen allows no window on top).
"""

from __future__ import annotations

import math
import queue
import sys
import threading
import tkinter as tk
from typing import Callable

from .detector import CubeDetector, EntityView, Snapshot

W, H = 300, 540
RADAR = 250
BG = "#101418"
FG = "#d8dee9"
DIM = "#6b7685"
KEY_CUBE = "#ff5c5c"
FREE_CUBE = "#ffd34d"
UNKNOWN_CUBE = "#ffa64d"
SPOT = "#4d9fff"
SPOT_NEXT = "#9fd0ff"
SPOT_DONE = "#3a4654"
OTHER = "#7a8899"
ALERT = "#ff8c1a"

COMPASS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
RULES = {
    "key": "Todos los cubos de este mapa necesitan llave",
    "mixed": "Cubos gratis, o con llave para mejor botín",
}
KIND_LABEL = {"key": "llave", "free": "gratis", None: ""}


def cube_color(kind: str | None) -> str:
    return {"key": KEY_CUBE, "free": FREE_CUBE}.get(kind, UNKNOWN_CUBE)


def compass(deg: float) -> str:
    return COMPASS[int((deg + 22.5) // 45) % 8]


def beep() -> None:
    if sys.platform == "win32":
        import winsound

        def play():
            for f in (880, 1320, 1760):
                winsound.Beep(f, 90)

        threading.Thread(target=play, daemon=True).start()


class Overlay:
    def __init__(self, detector: CubeDetector, on_close: Callable[[], None] | None = None):
        self.detector = detector
        self.config = detector.config
        self.on_close = on_close
        self.discovery = False
        self._actions: "queue.Queue[str]" = queue.Queue()
        self._alert_until = 0.0
        self._message = ""
        self._message_until = 0.0
        self._visible = True

        self.root = tk.Tk()
        self.root.title("Aion2 Helper")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        ov = self.config["overlay"]
        self.root.attributes("-alpha", ov.get("alpha", 0.85))
        self.root.geometry(f"{W}x{H}+{ov.get('x', 40)}+{ov.get('y', 200)}")
        self.root.configure(bg=BG)

        self.header = tk.Label(self.root, text="", bg=BG, fg=FG, font=("Segoe UI", 9, "bold"), anchor="w")
        self.header.pack(fill="x", padx=6, pady=(4, 0))
        self.status = tk.Label(self.root, text="", bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w", justify="left")
        self.status.pack(fill="x", padx=6)
        self.canvas = tk.Canvas(self.root, width=RADAR, height=RADAR, bg=BG, highlightthickness=2, highlightbackground=BG)
        self.canvas.pack(pady=4)
        self.listing = tk.Label(
            self.root, text="", bg=BG, fg=FG, font=("Consolas", 9), anchor="nw", justify="left"
        )
        self.listing.pack(fill="both", expand=True, padx=6)

        for w in (self.root, self.header, self.status):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
        self.root.bind("<Button-3>", lambda e: self.close())
        self._drag_from = (0, 0)

        if ov.get("click_through"):
            self.root.after(100, lambda: self._set_click_through(True))
        self.root.after(200, self._tick)

    # ───── called from other threads ─────

    def post(self, action: str) -> None:
        """Queues a hotkey action; safe from any thread."""
        self._actions.put(action)

    def alert(self) -> None:
        self._actions.put("alert")

    def learned(self, code: int) -> None:
        self._actions.put("learned")

    # ───── ui thread ─────

    def run(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        ov = self.config["overlay"]
        ov["x"], ov["y"] = self.root.winfo_x(), self.root.winfo_y()
        self.config.save()
        if self.on_close:
            self.on_close()
        self.root.destroy()

    def _flash(self, text: str, seconds: float = 3.0) -> None:
        self._message = text
        self._message_until = self.detector.clock() + seconds

    def _handle(self, action: str) -> None:
        if action == "alert":
            self._alert_until = self.detector.clock() + 4
            if self.config["sound"]:
                beep()
            else:
                self.root.bell()
        elif action == "mark":
            ok = self.detector.mark()
            self._flash("Marca guardada" if ok else "Sin registro: arranca con --log")
        elif action == "tag_nearest":
            e = self.detector.tag_nearest()
            self._flash(f"Código {e.code} marcado como cubo" if e else "Nada cerca para marcar")
        elif action == "click_through":
            ov = self.config["overlay"]
            ov["click_through"] = not ov.get("click_through")
            self._set_click_through(ov["click_through"])
            self._flash("Click-through " + ("ON" if ov["click_through"] else "OFF"))
        elif action == "toggle_overlay":
            self._visible = not self._visible
            (self.root.deiconify if self._visible else self.root.withdraw)()
        elif action == "reset_spots":
            n = self.detector.reset_spots()
            self._flash(f"{n} spots vuelven a estar por revisar")
        elif action == "learned":
            self._flash("Código del Hidden Cube aprendido solo", 6)
        elif action == "discovery":
            self.discovery = not self.discovery
            self._flash("Modo descubrimiento " + ("ON" if self.discovery else "OFF"))
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
        try:
            self._render(self.detector.snapshot())
        except Exception as exc:  # keep the overlay alive whatever happens
            self.status.configure(text=f"Error: {exc!r}")
        self.root.after(250, self._tick)

    def _render(self, snap: Snapshot) -> None:
        now = self.detector.clock()
        n = len(snap.cubes)
        calibrated = bool(self.config["cube_codes"]) or n > 0
        where = f"{snap.map_name} · " if snap.map_name else ""
        if calibrated:
            title = f"{where}Hidden Cubes cerca: {n}"
        elif self.detector.world is not None:
            title = f"{where}Hidden Cubes: aprendiendo el código…"
        else:
            title = "Hidden Cubes: sin calibrar (ver README)"
        color = cube_color(snap.cubes[0].kind) if n else FG
        self.header.configure(text=title, fg=color)
        pos = "aprendida" if snap.self_learned else "aprendiendo…"
        lines = [snap.status, snap.world, f"Posición propia: {pos} · entidades: {snap.entity_count}"]
        if snap.cube_rule in RULES:
            lines.append(RULES[snap.cube_rule])
        if now < self._message_until:
            lines.append(self._message)
        self.status.configure(text="\n".join(l for l in lines if l))
        border = ALERT if now < self._alert_until and int(now * 4) % 2 == 0 else BG
        self.canvas.configure(highlightbackground=border)
        self._draw_radar(snap)

        rows = []
        for v in snap.cubes[:5]:
            rows.append("◆ " + self._describe(v))
        if snap.next_spot is not None and snap.next_spot.distance_m is not None:
            ns = snap.next_spot
            rows.append(
                f"→ siguiente: {ns.distance_m:.0f} m {compass(self._screen_bearing(ns.bearing_deg))}"
                f" · {snap.pending_near} pendientes"
            )
        if self.discovery:
            rows.append("── cerca (F9 = es un cubo) ──")
            for v in snap.nearby:
                rows.append("· " + self._describe(v, code=True))
        if not rows:
            rows.append("Ningún cubo a la vista.")
        self.listing.configure(text="\n".join(rows))

    def _describe(self, v: EntityView, code: bool = False) -> str:
        parts = []
        if code:
            parts.append(f"{v.entity.code} k{v.entity.kind:02X}")
        if v.distance_m is not None:
            parts.append(f"{v.distance_m:5.0f} m {compass(self._screen_bearing(v.bearing_deg)):>2}")
        else:
            parts.append("dist. ?")
        if v.is_cube and KIND_LABEL.get(v.kind):
            parts.append(KIND_LABEL[v.kind])
        parts.append(f"hace {int(v.age_s)}s")
        return "  ".join(parts)

    def _screen_bearing(self, deg: float) -> float:
        b = -deg if self.config["radar_mirror"] else deg
        return (b + self.config["radar_rotation_deg"]) % 360

    def _draw_radar(self, snap: Snapshot) -> None:
        c = self.canvas
        c.delete("all")
        cx = cy = RADAR / 2
        r = RADAR / 2 - 6
        for k in (1, 2, 3):
            c.create_oval(cx - r * k / 3, cy - r * k / 3, cx + r * k / 3, cy + r * k / 3, outline="#253040")
        c.create_text(cx, 8, text="N", fill=DIM, font=("Segoe UI", 8))
        rng = self.config["radar_range_m"]
        c.create_text(RADAR - 4, RADAR - 6, text=f"{rng} m", fill=DIM, anchor="e", font=("Segoe UI", 7))
        c.create_polygon(cx, cy - 6, cx - 4, cy + 4, cx + 4, cy + 4, fill=FG)
        me = snap.self_pos
        if me is None:
            c.create_text(cx, cy + 22, text="sin posición propia", fill=DIM, font=("Segoe UI", 8))
            return
        upm = self.config["units_per_meter"]

        def plot(px: float, py: float) -> tuple[float, float, bool]:
            dist = math.hypot(px - me[0], py - me[1]) / upm
            deg = self._screen_bearing(math.degrees(math.atan2(py - me[1], px - me[0])))
            clipped = dist > rng
            rr = r if clipped else r * dist / rng
            a = math.radians(deg)
            return cx + rr * math.sin(a), cy - rr * math.cos(a), clipped

        for s in snap.spots:
            if s.status == "cube":
                continue  # drawn as a cube below
            x, y, clipped = plot(s.pos[0], s.pos[1])
            if clipped:
                continue
            if s.status == "empty":
                c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=SPOT_DONE, outline="")
            else:
                c.create_oval(x - 3, y - 3, x + 3, y + 3, outline=SPOT)
        ns = snap.next_spot
        if ns is not None:
            x, y, clipped = plot(ns.pos[0], ns.pos[1])
            if clipped:
                c.create_line(cx, cy, x, y, fill=SPOT_NEXT, dash=(3, 3), arrow="last")
            else:
                c.create_oval(x - 5, y - 5, x + 5, y + 5, outline=SPOT_NEXT, width=2)
        if self.discovery:
            for v in snap.nearby:
                if v.entity.pos is not None:
                    x, y, clipped = plot(v.entity.pos[0], v.entity.pos[1])
                    if not clipped:
                        c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=OTHER, outline="")
        for v in snap.cubes:
            if v.entity.pos is None:
                continue
            x, y, clipped = plot(v.entity.pos[0], v.entity.pos[1])
            size = 4 if clipped else 6
            col = cube_color(v.kind)
            c.create_polygon(x, y - size, x + size, y, x, y + size, x - size, y, fill=col, outline="")
            c.create_text(x, y + 12, text=f"{v.distance_m:.0f}m", fill=col, font=("Segoe UI", 8))

    # ───── window plumbing ─────

    def _drag_start(self, e) -> None:
        self._drag_from = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._drag_from[0]}+{e.y_root - self._drag_from[1]}")

    def _drag_end(self, e) -> None:
        ov = self.config["overlay"]
        ov["x"], ov["y"] = self.root.winfo_x(), self.root.winfo_y()

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
