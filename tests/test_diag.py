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
    assert "80 con posición" in report and "Posición propia: desconocida" in report
    assert "sin datos de la comunidad" in report


def test_report_explains_map_alignment(tmp_path):
    from aion2_helper.worldmap import WorldData

    det = CubeDetector(Config(None, {"world_transform": "xy"}))
    det.set_world(WorldData.from_dataset(community.build_dataset({"World_L_A": thgl_file(CUBES, ANCHORS)}, {})))
    for i, a in enumerate(ANCHORS):
        det.handle(Spawn(i + 1, 0x0C, 2_100_000, a), 1.0)
    report = "\n".join(det.diag.report(det))
    assert "Mapa: Verteron" in report and "World_L_A/xy=" in report
    assert "World_L_A/xy: 20 sobre un punto fijo" in report


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


def _live_frames(n_spawns: int):
    """Server -> client game segments as scapy dissects them off the wire, each followed by
    a bare ACK from the server padded to 60 bytes with non-zero junk (seen live 2026-10-14)."""
    from scapy.layers.inet import IP, TCP
    from scapy.layers.l2 import Ether

    seq = 5000
    out = []
    for i in range(n_spawns):
        body = frame(b"\x0e\x00\x36\x01\x02") + frame(spawn_body(800 + i, 2_100_744, (148066.0 + i, 96086.0, 28598.0)))
        data = Ether() / IP(src="193.202.112.94", dst="192.168.1.5") / TCP(sport=13328, dport=50000, seq=seq, flags="PA") / body
        out.append(Ether(bytes(data)))
        seq += len(body)
        ack = Ether() / IP(src="193.202.112.94", dst="192.168.1.5") / TCP(sport=13328, dport=50000, seq=seq, flags="A")
        out.append(Ether(bytes(ack) + b"\xbe\xde\x00\x03\x66\xfd"))
    return out


def test_ethernet_padding_of_bare_acks_does_not_corrupt_the_stream():
    from aion2_helper.capture import LiveCapture

    det = CubeDetector(Config(None))
    d = Dispatcher(det, DEFAULT_OPCODES)
    cap = LiveCapture(d)
    t = 100.0
    for pkt in _live_frames(40):
        pkt.time = t
        t += 0.02
        cap._on_packet(pkt)
    while not cap._q.empty():
        d.process(cap._q.get_nowait())
    assert d.locked
    assert det.diag.counts["spawns"] >= 28  # all of them after the lock
    assert det.diag.counts["tcp_dup_bytes"] == 0 and det.diag.counts["tcp_gaps"] == 0


def test_segment_from_frame_handles_link_layers_and_offload():
    import socket
    import struct as st

    from aion2_helper.capture import segment_from_frame

    def ip_tcp(payload: bytes, total: int | None = None) -> bytes:
        tcp = st.pack("!HHIIBBHHH", 13328, 50000, 7, 0, 5 << 4, 0x18, 65535, 0, 0)
        ln = 20 + len(tcp) + len(payload) if total is None else total
        ip = st.pack("!BBHHHBBH4s4s", 0x45, 0, ln, 0, 0, 64, 6, 0, socket.inet_aton("1.2.3.4"), socket.inet_aton("5.6.7.8"))
        return ip + tcp + payload

    eth = b"\x00" * 12 + b"\x08\x00"
    assert segment_from_frame(eth + ip_tcp(b"hi") + b"\xbe\xde\x00\x03", 0).data == b"hi"
    assert segment_from_frame(eth + ip_tcp(b"") + b"\xbe\xde\x00\x03\x66\xfd", 0) is None  # bare ACK
    assert segment_from_frame(eth + ip_tcp(b"hello", total=0), 0).data == b"hello"  # offload
    vlan = b"\x00" * 12 + b"\x81\x00\x00\x05\x08\x00"
    assert segment_from_frame(vlan + ip_tcp(b"hi"), 0).sport == 13328
    assert segment_from_frame(b"\x02\x00\x00\x00" + ip_tcp(b"hi"), 0).data == b"hi"
    assert segment_from_frame(ip_tcp(b"hi"), 0).dst == "5.6.7.8"


def _det(world=None, **cfg):
    from .test_detector import Clock

    clock = Clock()
    det = CubeDetector(Config(None, cfg), clock=clock)
    if world is not None:
        det.set_world(world)
    return det, clock


def test_teleport_gives_your_position_and_your_entity_is_learned():
    from aion2_helper.protocol import EntityMove, ZoneChange

    det, clock = _det()
    det.handle(ZoneChange((100000.0, 50000.0, 3000.0)))
    assert det.tracker.position == (100000.0, 50000.0, 3000.0) and det.self_source == "teletransporte"
    assert det.self_precise
    det.handle(EntityMove(4321, (100100.0, 50000.0, 3000.0)))  # near the teleport, not a spawned entity
    det.handle(EntityMove(4321, (100250.0, 50050.0, 3000.0)))
    assert det.self_id == 4321 and det.self_source == "servidor"
    clock.t += 30
    det.handle(EntityMove(4321, (120000.0, 60000.0, 3100.0)))
    assert det.tracker.position == (120000.0, 60000.0, 3100.0)
    assert det.snapshot().self_source == "servidor"


def test_parser_reads_teleport_moves_and_map_load():
    from aion2_helper.protocol import EntityMove, MapLoad, ZoneChange, encode_varint

    body_tp = b"\x23\x36\x00" + struct.pack("<fff", 106149.0, 171184.0, 24189.0) + b"\x00" * 4
    body_mv = b"\x23\x36" + encode_varint(5555) + struct.pack("<fff", 1000.0, 2000.0, 300.0)
    body_map = b"\x21\x36" + struct.pack("<I", 1) + struct.pack("<I", 110010000) + b"\x00" * 30
    events, _ = ServerStreamParser().consume(frame(body_tp) + frame(body_mv) + frame(body_map))
    assert events == [ZoneChange((106149.0, 171184.0, 24189.0)), EntityMove(5555, (1000.0, 2000.0, 300.0)), MapLoad(110010000)]


def test_map_id_is_learned_and_reused(tmp_path):
    from aion2_helper.protocol import MapLoad
    from aion2_helper.worldmap import WorldData

    w = WorldData.from_dataset(community.build_dataset({"World_L_A": thgl_file(CUBES, ANCHORS)}, {}))
    det, _ = _det(w, world_transform="xy")
    det.handle(MapLoad(110010000))
    for i, a in enumerate(ANCHORS):
        det.handle(Spawn(i + 1, 0x0C, 2_100_000, a))
    assert det.current_map.map_id == "World_L_A" and det.config["map_ids"]["110010000"] == "World_L_A"
    det.handle(MapLoad(220020000))
    assert det.current_map is None
    det.handle(MapLoad(110010000))
    assert det.current_map.map_id == "World_L_A"  # known id: no vote needed


def test_monsters_vote_by_their_own_code_with_swapped_axes():
    from aion2_helper.worldmap import WorldData, WorldLocator, to_game

    mobs = [(150000.0 + 9000 * i, 100000.0 - 7000 * i, 24000.0) for i in range(8)]
    raw = thgl_file(CUBES, ANCHORS[:1], mobs)  # every mob there has code m_1
    w = WorldData.from_dataset(community.build_dataset({"World_L_A": raw}, {}))
    assert 1 in w.maps["World_L_A"].monsters
    loc = WorldLocator(w)
    for p in mobs:  # wandered 25 m from their spawn points, axes swapped in the packets
        if loc.observe(to_game("yx", (p[0] + 2000, p[1] - 1500, p[2] + 150)), code=1):
            break
    assert (loc.map_id, loc.transform) == ("World_L_A", "yx")
    loc2 = WorldLocator(w)
    for p in mobs:  # another code at the same places: no vote
        loc2.observe(to_game("yx", (p[0] + 2000, p[1] - 1500, p[2] + 150)), code=2)
    assert loc2.map_id is None


def test_position_is_estimated_from_entities_when_nothing_better():
    det, clock = _det()
    for i in range(9):
        det.handle(Spawn(i + 1, 0x0C, 2_100_000, (50000.0 + 1000 * (i % 3), 20000.0 + 1000 * (i // 3), 500.0)))
    snap = det.snapshot()
    assert snap.self_source == "aproximada" and snap.self_pos == (51000.0, 21000.0, 500.0)
    assert not det.self_precise


def test_diagnostic_samples_known_opcodes():
    det = CubeDetector(Config(None))
    parser = ServerStreamParser(DEFAULT_OPCODES, det.diag)
    parser.consume(frame(b"\x1d\x37\x01\x02\x03\x04"))
    report = "\n".join(det.diag.report(det))
    assert "[1d 37]" in report and "1d 37 01 02 03 04" in report


# Real packets from a live diagnostic (Verteron, 2026-10-14), as the overlay sampled them.
LIVE_TELEPORT = "1e 23 36 00 00 00 00 00 00 be 1c 93 c6 64 30 0f 48 00 dd 09 47 01 00 00 00 97 52"
LIVE_MAP_LOAD = ("34 21 36 11 00 00 00 f2 03 00 00 5b 25 89 00 00 00 00 00 be 1c 93 c6 64 30 0f 48 00 dd 09 47"
                 " f9 cd 0b c3 02 00 00 00 00 00 00 00 00 00 00 00 00 00")
LIVE_MOVES = [
    "19 1a 37 c0 99 02 00 00 36 03 95 c6 42 b5 0f 48 00 d8 09 47 21 dc",
    "1b 1b 37 c0 99 02 04 18 be 95 c6 8e d1 0f 48 00 ce 09 47 1d dc 1d dc 01",
    "1b 1c 37 eb c4 02 04 3e d3 80 c7 d0 a1 e6 47 00 58 8e 46 5a ed 5a ed 01",
]


def _live(h: str) -> bytes:
    """The sample is the packet as framed (its length byte first); the stream needs the same bytes."""
    data = bytes.fromhex(h)
    from aion2_helper.protocol import read_varint

    value, vlen = read_varint(data, 0)
    return data + b"\x00" * max(0, value - 4 + vlen - len(data))  # samples are cut at 56 bytes


def test_live_teleport_map_load_and_moves_decode():
    import pytest

    from aion2_helper.protocol import EntityMove, MapLoad, ZoneChange

    stream = _live(LIVE_TELEPORT) + _live(LIVE_MAP_LOAD) + b"".join(_live(h) for h in LIVE_MOVES)
    events, consumed = ServerStreamParser().consume(stream)
    assert consumed == len(stream)
    tp, ml, m1, m2, m3 = events
    assert isinstance(tp, ZoneChange) and tp.pos == pytest.approx((-18830.4, 146625.6, 35293.0), abs=0.1)
    assert isinstance(ml, MapLoad) and ml.map_id == 1010 and ml.pos == tp.pos
    assert [type(e) for e in (m1, m2, m3)] == [EntityMove] * 3
    assert m1.entity_id == m2.entity_id == 36032 and m3.entity_id == 41579
    assert m1.pos == pytest.approx((-19073.6, 147157.0, 35288.0), abs=0.1)
    assert m3.pos == pytest.approx((-65958.5, 118083.6, 18220.0), abs=0.1)


def test_live_session_finds_you_and_verteron():
    det = CubeDetector(Config(None))
    parser = ServerStreamParser(DEFAULT_OPCODES, det.diag)
    events, _ = parser.consume(_live(LIVE_TELEPORT) + _live(LIVE_MAP_LOAD) + _live(LIVE_MOVES[0]) + _live(LIVE_MOVES[1]))
    for e in events:
        det.handle(e)
    assert det.map_number == 1010
    assert det.self_id == 36032 and det.self_source == "servidor"  # your entity: it moves where you landed


def test_spawns_in_a_bundle_inside_another_packet_are_read():
    inner = frame(spawn_body(901, 2_100_603, (74919.0, 95713.0, 11077.0)))
    bundle = b"\xff\xff" + struct.pack("<I", len(inner)) + lz4.block.compress(inner, store_size=False)
    from aion2_helper.protocol import encode_varint

    carrier = b"\x00\x8d\x01\x02\x03" + encode_varint(len(bundle) + 4) + bundle + b"\x05\x06"
    det = CubeDetector(Config(None))
    events, _ = ServerStreamParser(DEFAULT_OPCODES, det.diag).consume(frame(carrier))
    # (LZ4 keeps short data as literals, so the raw scan may see it too: same entity, no harm)
    assert {e.entity_id for e in events if isinstance(e, Spawn)} == {901}
    assert det.diag.counts["embedded_bundles"] == 1


def test_client_position_is_learned_even_after_a_varint_counter():
    """A counter before the position changes its offset from the start, not from the end."""
    from aion2_helper.protocol import ZoneChange, encode_varint

    det, clock = _det()
    det.handle(ZoneChange((-18830.0, 146625.0, 35293.0)), clock.t)  # the anchor: where you landed
    stream = b""
    for i in range(40):
        counter = encode_varint(100 + i * 7)  # 1 byte, then 2 bytes once past 127
        pos = struct.pack("<fff", -18830.0 + i * 40, 146625.0 + i * 25, 35293.0)
        stream += frame(b"\x12\x34" + counter + b"\x01" + pos + b"\x00\x00")
    t = clock.t
    buf = b""
    for i in range(0, len(stream), 32):  # arrives in pieces, kept until a frame is whole
        t += 0.1
        buf += stream[i : i + 32]
        buf = buf[det.tracker.feed(buf, t) :]
        det.note_self_position()
    assert det.tracker.locked and det.tracker.fixed_key[1] < 0
    clock.t = t
    snap = det.snapshot()
    assert snap.self_source == "cliente"
    assert snap.self_pos[0] > -18830.0 + 30 * 40
    assert snap.heading_deg is not None and 100 < snap.heading_deg < 140  # +X (east), +Y (south): ESE


def test_flag_1_removes_an_entity():
    from aion2_helper.protocol import encode_varint

    det = CubeDetector(Config(None))
    events, _ = ServerStreamParser().consume(frame(spawn_body(34342, 2_100_603, (1000.0, 2000.0, 300.0))) + frame(b"\x42\x36" + encode_varint(34342) + b"\x00\x01"))
    for e in events:
        det.handle(e, 1.0)
    assert det.entity_count == 0
