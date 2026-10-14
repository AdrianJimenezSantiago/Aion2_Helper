import json
import struct

from aion2_helper import analyze
from aion2_helper.capture import Dispatcher, PcapWriter, Segment, read_pcap, replay
from aion2_helper.config import Config
from aion2_helper.detector import CubeDetector
from aion2_helper.protocol import DEFAULT_OPCODES, Despawn, Spawn, ZoneChange, encode_varint, frame
from aion2_helper.selfpos import SelfPositionTracker
from aion2_helper.spots import SpotStore

from .test_protocol import spawn_body

CUBE = 7_000_123


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, codes=(CUBE,), **kw):
    cfg = Config(tmp_path / "config.json", {"cube_codes": list(codes)})
    clock = Clock()
    found = []
    det = CubeDetector(cfg, on_new_cube=found.append, clock=clock, spots=SpotStore(tmp_path / "spots.json"), **kw)
    return det, clock, found


def test_cube_appears_alerts_once_and_goes(tmp_path):
    det, clock, found = make(tmp_path)
    det.tracker.fixed_key = (b"\x00\x00", 0)
    det.tracker.position = (0.0, 0.0, 0.0)
    det.handle(Spawn(1, 0x0C, CUBE, (1000.0, 0.0, 0.0)))
    det.handle(Spawn(1, 0x0C, CUBE, (1000.0, 0.0, 0.0)))
    det.handle(Spawn(2, 0x0C, 555, (50.0, 0.0, 0.0)))
    assert len(found) == 1
    snap = det.snapshot()
    assert [v.entity.entity_id for v in snap.cubes] == [1]
    assert snap.cubes[0].distance_m == 10.0
    assert snap.cubes[0].bearing_deg == 0.0
    assert len(snap.spots) == 1
    det.handle(Despawn(1, 7))
    assert det.snapshot().cubes == []
    det.handle(ZoneChange())
    assert det.snapshot().entity_count == 0


def test_cube_ttl(tmp_path):
    det, clock, _ = make(tmp_path)
    det.handle(Spawn(1, 0x0C, CUBE, None))
    clock.t += det.config["cube_ttl_s"] + 1
    assert det.snapshot().cubes == []


def test_tag_nearest_learns_code(tmp_path):
    det, clock, _ = make(tmp_path, codes=())
    det.tracker.fixed_key = (b"\x00\x00", 0)
    det.tracker.position = (0.0, 0.0, 0.0)
    det.handle(Spawn(5, 0x0C, 111, (3000.0, 0.0, 0.0)))
    det.handle(Spawn(6, 0x0C, 222, (200.0, 100.0, 0.0)))
    assert det.tag_nearest().code == 222
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["cube_codes"] == [222]
    assert det.snapshot().cubes[0].entity.code == 222


def test_self_position_is_learned_from_client_packets(tmp_path):
    tr = SelfPositionTracker()
    tr.add_anchor((50000.0, 20000.0, 100.0))
    junk = SelfPositionTracker()  # no anchors: never locks
    t = 0.0
    for i in range(20):
        pos = (50000.0 + i * 100, 20000.0, 100.0)
        body = b"\x12\x34" + encode_varint(i) + b"\x01" + struct.pack("<fff", *pos) + b"\x00\x00"
        # A noisy packet type the tracker must not pick.
        noise = b"\x77\x01" + struct.pack("<fff", 9e5 * (i % 2 * 2 - 1), 1e5 * i, 5.0)
        stream = frame(body) + frame(noise)
        tr.feed(stream, t)
        junk.feed(stream, t)
        t += 0.25
    assert tr.locked and tr.fixed_key == (b"\x12\x34", 4)
    assert tr.position == (51900.0, 20000.0, 100.0)
    assert not junk.locked
    assert SelfPositionTracker.from_config(tr.key_for_config()).fixed_key == tr.fixed_key


def test_dispatcher_locks_and_detects_through_pcap(tmp_path):
    det, clock, found = make(tmp_path)
    pcap = tmp_path / "game.pcap"
    w = PcapWriter(pcap)
    server, client = ("10.0.0.1", 4000), ("192.168.1.2", 50000)
    seq = 1
    t = 100.0
    for i in range(15):  # game traffic carrying the signature, so the flow locks
        data = frame(b"\x10\x38\x0e\x00\x36" + bytes([i]))
        w.write(Segment(t, *server, *client, seq, data))
        seq += len(data)
        t += 0.1
    spawn = frame(spawn_body(4242, CUBE))
    w.write(Segment(t, *server, *client, seq, spawn[:10]))  # split across segments
    w.write(Segment(t, *server, *client, seq + 10, spawn[10:]))
    w.close()

    assert len(list(read_pcap(pcap))) == 17
    disp = Dispatcher(det, DEFAULT_OPCODES)
    replay(pcap, disp)
    assert disp.locked
    assert [e.entity_id for e in found] == [4242]


def test_analyze_ranks_cube_code(tmp_path):
    log = tmp_path / "d.jsonl"
    recs = []
    for k in range(4):
        base = k * 1000.0
        me = [base, 0.0, 0.0]
        recs.append({"type": "spawn", "t": base, "id": k, "code": CUBE, "kind": 12, "pos": [base + 100, 0, 0]})
        for j in range(5):
            recs.append({"type": "spawn", "t": base + j, "id": 100 + j, "code": 2920548, "kind": 12, "pos": [base + 5000, 0, 0]})
        recs.append({"type": "mark", "t": base + 30, "self": me})
    log.write_text("\n".join(json.dumps(r) for r in recs))
    assert analyze.rank(analyze.load(log))[0][0] == CUBE
