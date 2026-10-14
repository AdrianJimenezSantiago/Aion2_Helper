"""Hidden Cube spots from the community map at aion2.th.gl.

aion2.th.gl (The Hidden Gaming Lair) builds its map from the game client's own
data: every marker is a world position in Unreal units, the same coordinates
the server sends us. The markers of each map are one static file on its CDN:

    https://aion2.th.gl/maps/Verteron       the map page; lists the current files in "nodesPaths"
    https://cdn.th.gl/aion2/nodes/World_L_A.<hash>.raw    cbor-x document (see cborx.py)

Each file is a list of groups ``{"type", "mapName", "static", "spawns"}`` and
each spawn has ``"p": [x, y, z]``. The ``hidden_cube`` groups hold the 3 722
cube spots (Verteron 1 440, Altgard 1 371, Eltnen 456, Morheim 455). The other
fixed points (gathering nodes, NPCs, portals...) are kept as "anchors" to tell
which map we are on.

Nothing is bundled with this program: ``update-spots`` downloads the current
files and keeps a compact copy next to the config. The data is credited to
aion2.th.gl wherever it is shown.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import cborx

SOURCE = "aion2.th.gl"
MAP_PAGE = "https://aion2.th.gl/maps/Verteron"
CDN = "https://cdn.th.gl/aion2"
USER_AGENT = "Aion2Helper/0.3.0 (+https://github.com/adrianjimenezsantiago/aion2_helper)"
NODES_RE = re.compile(r"/nodes/(World_[A-Za-z_]+)\.([0-9a-f]{16,64})\.raw")
# Known files at the time of writing, used only if the map page cannot be read.
FALLBACK_PATHS = {
    "World_L_A": "/nodes/World_L_A.ee18b5cf3f0c3988a65ba42577d362d6.raw",
    "World_L_B": "/nodes/World_L_B.e5cb431cdadab1581ddad36c9c1df919.raw",
    "World_D_A": "/nodes/World_D_A.ed8efe5fe72d9b882eca12b81bca09ba.raw",
    "World_D_B": "/nodes/World_D_B.3e59e6a568029b64ad51b57dbacea569.raw",
    "World_L_Starter": "/nodes/World_L_Starter.db4f4d9e3aa4dcd482f1095af6095b38.raw",
    "World_D_Starter": "/nodes/World_D_Starter.0270db006333f7a85bd2a5d60960189a.raw",
}
CUBE_TYPE = "hidden_cube"
# Points that move or cover an area are no good for telling maps apart.
NOT_ANCHORS = ("monsters_", "field_boss")
MAX_DOWNLOAD = 20_000_000


def dataset_path(config_path: Path | None) -> Path:
    base = config_path.parent if config_path else Path.cwd()
    return base / "community" / "thgl-aion2.json"


# ───── download ─────


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise ValueError(f"{url}: respuesta demasiado grande")
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data


def find_node_paths(page: str) -> dict[str, str]:
    """The current node file of each map, as listed in the map page."""
    return {m.group(1): m.group(0) for m in NODES_RE.finditer(page.replace("\\/", "/"))}


def _get_retry(url: str, timeout: float, tries: int = 3) -> bytes:
    for attempt in range(tries):
        try:
            return _get(url, timeout)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def download(timeout: float = 30.0, log=print) -> dict:
    """Downloads the current node files and returns the compact dataset."""
    try:
        paths = find_node_paths(_get_retry(MAP_PAGE, timeout).decode("utf-8", "replace"))
    except Exception as exc:
        log(f"No se pudo leer {MAP_PAGE} ({exc}); uso las rutas conocidas")
        paths = {}
    if not paths:
        paths = dict(FALLBACK_PATHS)
    raw: dict[str, bytes] = {}
    for map_id, path in sorted(paths.items()):
        try:
            raw[map_id] = _get_retry(CDN + path, timeout)
        except Exception as exc:
            log(f"{map_id}: no se pudo descargar ({exc})")
    if not raw:
        raise RuntimeError("no se pudo descargar ningún mapa de " + SOURCE)
    return build_dataset(raw, {m: CDN + p for m, p in paths.items() if m in raw})


def import_dir(folder: Path) -> dict:
    """Builds the dataset from ``World_*.raw`` files downloaded by hand."""
    raw = {}
    for f in sorted(folder.glob("World_*.raw")):
        raw[f.name.split(".")[0]] = f.read_bytes()
    if not raw:
        raise FileNotFoundError(f"no hay ficheros World_*.raw en {folder}")
    return build_dataset(raw, {m: str(folder / m) for m in raw})


# ───── conversion ─────


def _points(group: dict) -> list[list[float]]:
    out = []
    for s in group.get("spawns") or []:
        p = s.get("p") if isinstance(s, dict) else None
        if isinstance(p, list) and len(p) >= 2 and all(isinstance(v, (int, float)) for v in p[:3]):
            out.append([round(float(v), 1) for v in (p + [0])[:3]])
    return out


def parse_nodes(raw: bytes) -> tuple[list[list[float]], list[list[float]]]:
    """(cube spots, anchors) of one map file."""
    doc = cborx.loads(raw)
    if not isinstance(doc, list):
        raise ValueError("formato inesperado: se esperaba una lista de grupos")
    cubes: list[list[float]] = []
    anchors: list[list[float]] = []
    for g in doc:
        if not isinstance(g, dict):
            continue
        kind = str(g.get("type", ""))
        if kind == CUBE_TYPE:
            cubes += _points(g)
        elif not kind.startswith(NOT_ANCHORS):
            anchors += _points(g)
    return cubes, anchors


def build_dataset(raw_by_map: dict[str, bytes], files: dict[str, str]) -> dict:
    maps = {}
    for map_id, raw in raw_by_map.items():
        cubes, anchors = parse_nodes(raw)
        maps[map_id] = {"cubes": cubes, "anchors": anchors}
    return {
        "version": 1,
        "source": SOURCE,
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
        "maps": maps,
    }


# ───── local copy ─────


def save(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("maps") else None


def age_days(data: dict) -> float:
    try:
        t = datetime.strptime(data["fetched"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return float("inf")
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def summary(data: dict) -> list[str]:
    from .worldmap import MAPS

    lines = []
    for map_id, m in sorted(data["maps"].items()):
        name, rule = MAPS.get(map_id, (map_id, None))
        rule_txt = {"key": "todos con llave", "mixed": "gratis o con llave"}.get(rule, "")
        lines.append(f"  {name:<24} {len(m['cubes']):>5} spots de cubo  {len(m['anchors']):>5} puntos fijos  {rule_txt}")
    total = sum(len(m["cubes"]) for m in data["maps"].values())
    lines.append(f"  Total: {total} spots de Hidden Cube (fuente: {data.get('source')}, {data.get('fetched')})")
    return lines
