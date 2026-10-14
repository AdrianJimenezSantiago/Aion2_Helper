"""Settings, kept as JSON so opcodes and cube codes can be fixed after a patch without a new build."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

DEFAULTS: dict = {
    # NPC codes (decimal) of the Hidden Cube objects. Learned by themselves from the community
    # spots (see "auto_cube_codes"); the "tag" hotkey and `analyze` still work. See the README.
    "cube_codes": [],
    # Codes whose entities keep appearing on known cube spots are added to cube_codes.
    "auto_cube_codes": True,
    # Evidence gathered so far for that (filled in by the program).
    "cube_candidates": {},
    # "key" or "free" per code (as a string). Codes seen on a key-only map are filled in as "key".
    "cube_kinds": {},
    # Community spots (aion2.th.gl): downloaded at start-up when missing or older than this.
    "community": {"auto_update": True, "max_age_days": 7},
    # Axis convention between packets and community data; found by itself (expected "xy").
    "world_transform": None,
    # A known spot you come this close to without a cube on it counts as checked (refined by itself) ...
    "spot_check_radius_m": 40,
    # ... and is shown as pending again after this many seconds.
    "spot_recheck_s": 1800,
    # Codes never shown in the discovery list (mobs you keep seeing and know are not cubes).
    "ignore_codes": [],
    "opcodes": {
        "spawn": ["41 36"],
        "despawn": ["42 36"],
        "zone_change": ["23 36"],
        "signatures": ["0E 00 36", "06 00 36"],
    },
    # Learned automatically; set by hand only if auto-learning picks the wrong field.
    "self_position": None,
    # A cube the server never removed is dropped after this many seconds.
    "cube_ttl_s": 600,
    "units_per_meter": 100,
    "radar_range_m": 60,
    # Rotate / mirror the radar if north does not point up in your game.
    "radar_rotation_deg": 0,
    "radar_mirror": False,
    # The game's process name; capture is ignored while it is not running (Windows only).
    "game_process": "AION2",
    "sound": True,
    "overlay": {"x": 40, "y": 200, "alpha": 0.85, "click_through": False},
    "hotkeys": {
        "mark": "<f8>",
        "tag_nearest": "<f9>",
        "click_through": "<f10>",
        "toggle_overlay": "<f11>",
        "discovery": "<f7>",
        "reset_spots": "<f6>",
    },
}


def default_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Aion2Helper" / "config.json"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path: Path | None = None, data: dict | None = None):
        self.path = path
        self.data = _merge(DEFAULTS, data or {})

    @classmethod
    def load(cls, path: Path | None) -> "Config":
        path = path or default_path()
        data = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        return cls(path, data)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def __getitem__(self, key: str):
        return self.data[key]

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value
