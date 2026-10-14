import json
import struct

from aion2_helper import analyze, cborx, community
from aion2_helper.config import Config
from aion2_helper.detector import CubeDetector
from aion2_helper.protocol import Spawn, ZoneChange
from aion2_helper.spots import SpotStore
from aion2_helper.worldmap import CubeCodeLearner, WorldData, WorldLocator, to_data, to_game

from .test_detector import Clock

# ───── a tiny CBOR writer, enough to build cbor-x style files ─────


def head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([major << 5 | n])
    if n < 256:
        return bytes([major << 5 | 24, n])
    if n < 65536:
        return bytes([major << 5 | 25]) + struct.pack(">H", n)
    return bytes([major << 5 | 26]) + struct.pack(">I", n)


def enc(v) -> bytes:
    if isinstance(v, Raw):
        return v.data
    if v is True:
        return b"\xf5"
    if v is False:
        return b"\xf4"
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, float):
        return b"\xfb" + struct.pack(">d", v)
    if isinstance(v, str):
        b = v.encode()
        return head(3, len(b)) + b
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(enc(x) for x in v)
    if isinstance(v, dict):
        return head(5, len(v)) + b"".join(enc(k) + enc(x) for k, x in v.items())
    raise TypeError(v)


class Raw:
    def __init__(self, data: bytes):
        self.data = data


def tag(n: int, value) -> Raw:
    return Raw(head(6, n) + enc(value))


def ref(k: int) -> Raw:
    """Packed reference to table slot 16 + 2k (k >= 0)."""
    return tag(6, k)


def record_def(rid: int, keys: list, values: list) -> Raw:
    return tag(0xDFFF, [rid, keys] + values)


def record(rid: int, values: list) -> Raw:
    return tag(rid, values)


def thgl_file(cubes, anchors, mobs=()) -> bytes:
    """A node file shaped like aion2.th.gl's: packed table, records, references."""
    table = ["icons.webp"] + ["x"] * 15 + ["World_L_A", "unused"]  # slot 16 = map name
    spawns_cube = [record_def(0xE001, ["p", "id"], [list(cubes[0]), "hidden_cube@0"])] + [
        record(0xE001, [list(p), f"hidden_cube@{i}"]) for i, p in enumerate(cubes[1:], 1)
    ]
    groups = [
        record_def(0xE000, ["type", "mapName", "spawns", "static"], ["hidden_cube", ref(0), spawns_cube, True]),
        record(0xE000, ["gather_odyle", ref(0), [{"p": list(p)} for p in anchors], True]),
        record(0xE000, ["monsters_fera", ref(0), [{"p": list(p), "dbEntryId": "m_1"} for p in mobs], False]),
    ]
    return tag(51, [table, [], [], groups]).data


CUBES = [(1000.0, 2000.0, 300.0), (50000.0, -20000.0, 1200.0), (-30000.0, 40000.0, 800.0)]
ANCHORS = [(1000.0 + 3000 * i, 5000.0 - 1500 * i, 300.0 + 10 * i) for i in range(20)]


def test_cborx_decodes_records_and_packed_values():
    doc = cborx.loads(thgl_file(CUBES, ANCHORS[:2], [(9.0, 9.0, 9.0)]))
    assert [g["type"] for g in doc] == ["hidden_cube", "gather_odyle", "monsters_fera"]
    assert all(g["mapName"] == "World_L_A" for g in doc)
    assert doc[0]["spawns"][1] == {"p": [50000.0, -20000.0, 1200.0], "id": "hidden_cube@1"}
    assert doc[1]["spawns"][0] == {"p": [1000.0, 5000.0, 300.0]}


def test_cborx_simple_value_reference_and_floats():
    data = tag(51, [["a"] * 3 + ["b"], [], [], [Raw(b"\xe3"), Raw(b"\xf9\x3c\x00"), Raw(b"\xfa\x40\x20\x00\x00")]]).data
    assert cborx.loads(data) == ["b", 1.0, 2.5]


def test_parse_nodes_splits_cubes_and_anchors():
    cubes, anchors = community.parse_nodes(thgl_file(CUBES, ANCHORS, [(9.0, 9.0, 9.0)]))
    assert cubes == [list(p) for p in CUBES]
    assert anchors == [list(p) for p in ANCHORS]  # monsters are left out


def test_find_node_paths_in_map_page():
    page = 'x\\"nodesPaths\\":{\\"World_L_A\\":\\"/nodes/World_L_A.ee18b5cf3f0c3988a65ba42577d362d6.raw\\"}'
    assert community.find_node_paths(page) == {"World_L_A": "/nodes/World_L_A.ee18b5cf3f0c3988a65ba42577d362d6.raw"}


def world(tmp_path=None) -> WorldData:
    raw = {
        "World_L_A": thgl_file(CUBES, ANCHORS),
        "World_D_B": thgl_file([(c[0] + 7777, c[1], c[2]) for c in CUBES], [(a[0] + 50000, a[1] + 33333, a[2]) for a in ANCHORS]),
    }
    data = community.build_dataset(raw, {})
    if tmp_path is not None:
        community.save(data, tmp_path / "w.json")
        data = community.load(tmp_path / "w.json")
    return WorldData.from_dataset(data)


def test_dataset_roundtrip(tmp_path):
    w = world(tmp_path)
    assert w.cube_count == 6 and set(w.maps) == {"World_L_A", "World_D_B"}
    assert w.maps["World_L_A"].name == "Verteron" and w.maps["World_L_A"].cube_rule == "key"
    assert w.maps["World_D_B"].cube_rule == "mixed"


def test_locator_finds_map_and_axes():
    for tf in ("xy", "x-y", "-yx"):
        loc = WorldLocator(world())
        for i, a in enumerate(ANCHORS * 2):
            if loc.observe(to_game(tf, (a[0] + 50, a[1] - 40, a[2] + 100))):
                break
        assert (loc.map_id, loc.transform) == ("World_L_A", tf), tf
        assert loc.height is not None


def test_locator_falls_back_to_2d_when_heights_never_match():
    loc = WorldLocator(world(), "xy")
    for i in range(200):
        a = ANCHORS[i % len(ANCHORS)]
        if loc.observe((a[0], a[1], a[2] + 5000)):
            break
    assert loc.map_id == "World_L_A" and loc.height is None


def test_learner_persists_counts():
    lr = CubeCodeLearner()
    lr.observe(5, 1, ("World_L_A", 0))
    assert lr.is_probable(5) and not lr.is_confirmed(5)
    lr.observe(5, 2, ("World_L_A", 1))
    lr.observe(9, 3, ("World_L_A", 2))
    for i in range(10):
        lr.observe(9, 10 + i, None)
    assert lr.is_confirmed(5) and not lr.is_probable(9)
    again = CubeCodeLearner(json.loads(json.dumps(lr.to_config())))
    again.new_session()
    again.observe(5, 1, None)  # same id, new connection: a different entity
    assert again.stats[5].total == 3 and again.is_confirmed(5)
    assert again.precision(9) == 1 / 11


def detector(tmp_path, **cfg):
    config = Config(tmp_path / "config.json", {"world_transform": "xy", **cfg})
    clock = Clock()
    found = []
    det = CubeDetector(config, on_new_cube=found.append, clock=clock, spots=SpotStore(None))
    det.set_world(world())
    return det, clock, found


def locate(det, start_id=10_000):
    for i, a in enumerate(ANCHORS):
        det.handle(Spawn(start_id + i, 0x0C, 2_100_000 + i % 3, (a[0], a[1], a[2] + 50)))
    assert det.current_map.map_id == "World_L_A"


def test_detector_learns_cube_code_by_itself(tmp_path):
    det, clock, found = detector(tmp_path)
    learned = []
    det.on_learned = learned.append
    locate(det)
    det.handle(Spawn(1, 0x0C, 3_000_001, (CUBES[0][0] + 80, CUBES[0][1], CUBES[0][2] + 40)))
    assert [e.entity_id for e in found] == [1]  # probable already: alerted at once
    assert det.config["cube_codes"] == []
    det.handle(Spawn(2, 0x0C, 3_000_001, CUBES[1]))
    assert det.config["cube_codes"] == [3_000_001] and learned == [3_000_001]
    assert det.config["cube_kinds"] == {"3000001": "key"}
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["cube_codes"] == [3_000_001] and saved["world_transform"] == "xy"
    snap = det.snapshot()
    assert snap.map_name == "Verteron" and snap.cube_rule == "key"
    assert {v.kind for v in snap.cubes} == {"key"}


def test_mob_on_a_spot_by_chance_is_not_a_cube(tmp_path):
    det, clock, found = detector(tmp_path)
    locate(det)
    for i in range(30):
        det.handle(Spawn(100 + i, 0x0C, 2_222_222, (90000.0 + 900 * i, 0.0, 0.0)))
    det.handle(Spawn(200, 0x0C, 2_222_222, CUBES[0]))
    det.handle(Spawn(201, 0x0C, 2_222_222, CUBES[1]))
    assert found == [] and det.config["cube_codes"] == [] and det.config["cube_kinds"] == {}


def test_spots_are_checked_off_and_next_spot_guides(tmp_path):
    det, clock, _ = detector(tmp_path, radar_range_m=100)
    locate(det)
    det.tracker.fixed_key = (b"\x00\x00", 0)
    det.tracker.position = (CUBES[0][0] + 1000, CUBES[0][1], CUBES[0][2])  # 10 m from spot 0
    snap = det.snapshot()
    near = [s for s in snap.spots if s.distance_m is not None and s.distance_m < 20]
    assert [s.status for s in near] == ["empty"]
    assert snap.next_spot is not None and snap.next_spot.pos[:2] == CUBES[2][:2]  # the nearest pending one
    clock.t += det.config["spot_recheck_s"] + 1
    det.tracker.position = (1000.0, 30000.0, 0.0)  # 280 m away: spot 0 is pending again
    assert det.snapshot().next_spot.pos[:2] == CUBES[0][:2]
    det.tracker.position = (CUBES[0][0] + 1000, CUBES[0][1], CUBES[0][2])
    det.snapshot()
    assert det.reset_spots() == 1


def test_cube_at_spot_shows_as_cube_not_empty(tmp_path):
    det, clock, _ = detector(tmp_path, cube_codes=[3_000_001])
    locate(det)
    det.tracker.fixed_key = (b"\x00\x00", 0)
    det.tracker.position = (CUBES[0][0] + 500, CUBES[0][1], CUBES[0][2])
    det.handle(Spawn(1, 0x0C, 3_000_001, CUBES[0]))
    statuses = {s.status for s in det.snapshot().spots if s.distance_m < 20}
    assert statuses == {"cube"}


def test_zone_change_keeps_map_but_new_connection_forgets_it(tmp_path):
    det, _, _ = detector(tmp_path)
    locate(det)
    det.handle(ZoneChange())  # may be just a sub-area: the map vote carries on
    assert det.current_map.map_id == "World_L_A"
    det.new_connection()
    assert det.current_map is None and det.locator.transform == "xy"


def test_locator_moves_to_another_map_when_it_clearly_wins():
    w = world()
    loc = WorldLocator(w, "xy")
    for a in ANCHORS:
        loc.observe(a)
    assert loc.map_id == "World_L_A"
    other = [p for p in w.maps["World_D_B"].anchors.points]
    changed = False
    for _ in range(3):
        for a in other:
            changed |= loc.observe(a)
    assert changed and loc.map_id == "World_D_B"
    assert loc.top_scores(1)[0][0] == ("World_D_B", "xy")


def test_analyze_ranks_codes_by_spots():
    recs = [{"type": "zone", "t": 0}]
    for i, a in enumerate(ANCHORS):
        recs.append({"type": "spawn", "t": i, "id": 10 + i, "code": 2_100_000, "pos": list(a)})
    for i, c in enumerate(CUBES):
        recs.append({"type": "spawn", "t": 50 + i, "id": 100 + i, "code": 3_000_001, "pos": list(c)})
    ranked, maps = analyze.rank_by_spots(recs, world())
    assert maps == ["Verteron"]
    assert ranked[0][:3] == (3_000_001, 3, 3)


def test_transform_inverse():
    for p in [(1.0, 2.0, 3.0), (-5.0, 7.0, 0.0)]:
        for tf in ("xy", "x-y", "-xy", "-x-y", "yx", "y-x", "-yx", "-y-x"):
            assert to_game(tf, to_data(tf, p)) == p
