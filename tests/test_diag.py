import json
import struct

import lz4.block

from aion2_helper import __main__ as cli
from aion2_helper import community
from aion2_helper.capture import Dispatcher, PcapWriter, Segment
from aion2_helper.config import Config
from aion2_helper.detector import CubeDetector
from aion2_helper.protocol import DEFAULT_OPCODES, ServerStreamParser, Spawn, frame, walk

from .test_protocol import spawn_body
from .test_worldmap import ANCHORS, CUBES, thgl_file


def big_bundle(n_spawns: int) -> bytes:
    """An LZ4 bundle over 16 KB on the wire, like the zone-load one."""
    import os

    inner = b"".join(frame(spawn_body(1000 + i, 2_100_000 + i, (1000.0 * (i + 1), 0.0, 0.0)) + os.urandom(300)) for i in range(n_spawns))
    body = b"\xff\xff" + struct.pack("<I", len(inner)) + lz4.block.compress(inner, store_size=False)
    return frame(body)


def test_walk_waits_for_a_bundle_bigger_than_16k():
    data = big_bundle(80)
    assert len(data) > 16384
    frames, consumed = walk(data[:9000])
    assert frames == [] and consumed == 0  # waits: it is a bundle header, not garbage
    frames, consumed = walk(data)
    assert len(frames) == 1 and frames[0].bundle and consumed == len(data)


def test_walk_still_skips_a_garbage_length():
    junk = bytes([0xFF, 0xFF, 0x01]) + b"\x00\x11" * 10  # varint 0x7FFF... no: a big length, no FF FF after it
    frames, consumed = walk(bytes([0x80, 0x90, 0x01, 0x41, 0x41, 0x41, 0x41, 0x41, 0x41]) + junk)
    assert consumed > 0


def test_parser_counts_each_stage():
    det = CubeDetector(Config(None))
    parser = ServerStreamParser(DEFAULT_OPCODES, det.diag)
    events, _ = parser.consume(big_bundle(80) + frame(b"\x23\x36\x00"))
    for e in events:
        det.handle(e, 1.0)
    c = det.diag.counts
    assert c["bundles"] == 1 and c["frames"] == 81 and c["spawns"] == 80 and c["zone_changes"] == 1
    assert "41 36" in dict(det.diag.opcodes)
    report = "\n".join(det.diag.report(det))
    assert "80 con posición" in report and "Posición propia: buscando" in report
    assert "sin datos de la comunidad" in report


def test_report_explains_map_alignment(tmp_path):
    from aion2_helper.worldmap import WorldData

    det = CubeDetector(Config(None, {"world_transform": "xy"}))
    det.set_world(WorldData.from_dataset(community.build_dataset({"World_L_A": thgl_file(CUBES, ANCHORS)}, {})))
    for i, a in enumerate(ANCHORS):
        det.handle(Spawn(i + 1, 0x0C, 2_100_000, a), 1.0)
    report = "\n".join(det.diag.report(det))
    assert "Mapa: Verteron" in report and "World_L_A/xy=" in report
    assert "World_L_A: 20 sobre un punto fijo" in report


def test_old_bare_f_key_hotkeys_are_migrated(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hotkeys": {"mark": "<f8>", "discovery": "<alt>+d"}}))
    cfg = Config.load(path)
    assert cfg["hotkeys"]["mark"] == "<ctrl>+<shift>+<f8>"
    assert cfg["hotkeys"]["discovery"] == "<alt>+d"  # chosen by the user: kept


def test_diagnose_command_reports_a_recording(tmp_path, capsys):
    pcap = tmp_path / "game.pcap"
    w = PcapWriter(pcap)
    server, client = ("10.0.0.1", 7777), ("192.168.1.2", 50000)
    seq = 1
    for i in range(30):
        data = frame(spawn_body(500 + i, 2_200_000, (100.0 * i, 50.0, 10.0))) + frame(b"\x0e\x00\x36\x01")
        w.write(Segment(1000.0 + i * 0.05, *server, *client, seq, data))
        seq += len(data)
    w.close()
    (tmp_path / "config.json").write_text("{}")
    assert cli.main(["--config", str(tmp_path / "config.json"), "diagnose", str(pcap)]) == 0
    out = capsys.readouterr().out
    assert "30 segmentos TCP" in out and "conexiones fijadas" in out and "Spawns:" in out


def test_dispatcher_counts_segments():
    det = CubeDetector(Config(None))
    d = Dispatcher(det, DEFAULT_OPCODES)
    for i in range(14):
        d.process(Segment(float(i) * 0.1, "10.0.0.1", 7777, "10.0.0.2", 5000, 1 + i * 10, frame(b"\x0e\x00\x36\x01\x02\x03")))
    assert det.diag.counts["conn_locks"] == 1 and det.diag.counts["seg_down"] >= 2


def test_tls_connection_on_the_same_server_port_is_not_mixed_in():
    """The game server also speaks TLS from its port on a second connection (seen live 2026-10-14)."""
    import os

    from aion2_helper.protocol import Spawn as SpawnEvent

    det = CubeDetector(Config(None))
    d = Dispatcher(det, DEFAULT_OPCODES)
    server = ("193.202.112.94", 13328)
    game_seq, tls_seq = 1000, 900_000_000
    spawned = []
    original = det.handle
    det.handle = lambda e, t=None: (spawned.append(e) if isinstance(e, SpawnEvent) else None, original(e, t))
    for i in range(40):
        t = 10.0 + i * 0.05
        tls = bytes([0x17, 0x03, 0x03, 0x00, 0x40]) + os.urandom(0x40)
        d.process(Segment(t, *server, "192.168.1.5", 50001, tls_seq, tls))
        tls_seq += len(tls)
        game = frame(b"\x0e\x00\x36\x01\x02") + frame(spawn_body(700 + i, 2_100_603, (74919.0 + i, 95713.0, 11077.0)))
        d.process(Segment(t + 0.01, *server, "192.168.1.5", 50000, game_seq, game))
        game_seq += len(game)
    assert d.locked and d._flow == ("193.202.112.94", 13328, "192.168.1.5", 50000)
    assert det.diag.counts["seg_other"] >= 25
    assert len(spawned) >= 28  # every spawn after the lock is read; none lost to the TLS bytes


def test_game_moving_to_a_new_connection_is_followed():
    det = CubeDetector(Config(None))
    d = Dispatcher(det, DEFAULT_OPCODES)
    seq = 1
    for i in range(14):
        d.process(Segment(i * 0.1, "10.0.0.1", 7777, "10.0.0.2", 5000, seq, frame(b"\x0e\x00\x36\x01")))
        seq += 6
    assert d._flow[3] == 5000
    for i in range(14):  # 10 s later, a reconnect from a new client port
        d.process(Segment(10 + i * 0.1, "10.0.0.1", 7777, "10.0.0.2", 5002, 1 + i * 6, frame(b"\x0e\x00\x36\x01")))
    assert d._flow[3] == 5002 and det.diag.counts["conn_locks"] == 2
