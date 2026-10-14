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
    from aion2_helper.protocol import EntityMove, SelfStats, ZoneChange

    det, clock = _det()
    det.handle(ZoneChange((100000.0, 50000.0, 3000.0)))
    assert det.tracker.position == (100000.0, 50000.0, 3000.0) and det.self_source == "teletransporte"
    assert det.self_precise
    det.handle(SelfStats(4321))  # your stats: only ever about you
    det.handle(EntityMove(4321, (100100.0, 50000.0, 3000.0)))
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
    # 36032 moved beside you there, but later got a removal: not you. Two moves are not enough to say so.
    assert det.self_id is None


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


# Second live session (2026-10-14): your own entity (ac 12 = 2348) with flags 05 03.
LIVE2_TELEPORT = "1e 23 36 00 00 00 00 00 00 be 1c 94 c6 64 70 0f 48 00 c8 09 47 02 00 00 00 82 2d"
LIVE2_SELF_MOVES = [
    "1a 1b 37 ac 12 05 03 be 1c 92 c6 64 70 0f 48 38 f7 09 47 b6 9b b6 9b",
    "1a 1c 37 ac 12 05 03 74 e2 95 c6 4d 43 0f 48 23 ab 09 47 9f 99 62 99",
]
LIVE2_OTHER_MOVE = "1a 1b 37 89 5b 01 01 1a 15 9b c6 b9 d3 0d 48 aa 02 04 47 f8 e5 f8 e5"


def test_live_flags_byte_decides_where_the_position_is():
    import pytest

    from aion2_helper.protocol import EntityMove

    events, _ = ServerStreamParser().consume(b"".join(_live(h) for h in LIVE2_SELF_MOVES + [LIVE2_OTHER_MOVE]))
    assert [type(e) for e in events] == [EntityMove] * 3
    assert [e.entity_id for e in events] == [2348, 2348, 11657]
    assert events[0].pos == pytest.approx((-18702.4, 146881.6, 35319.2), abs=0.1)
    assert events[1].pos == pytest.approx((-19185.2, 146701.2, 35243.1), abs=0.1)
    assert events[2].pos == pytest.approx((-19850.6, 145230.9, 33794.7), abs=0.1)


def test_live_your_entity_is_found_and_followed():
    from aion2_helper.protocol import SelfStats

    det, clock = _det()
    det.handle(SelfStats(2348))
    for e in ServerStreamParser().consume(_live(LIVE2_TELEPORT) + b"".join(_live(h) for h in LIVE2_SELF_MOVES))[0]:
        clock.t += 0.5
        det.handle(e)
    assert det.self_id == 2348 and det.self_source == "servidor"
    clock.t += 30  # no record of yours for a while: the last position holds
    snap = det.snapshot()
    assert snap.self_source == "servidor" and snap.self_pos[0] == pytest_approx(-19185.2)


def pytest_approx(v):
    import pytest

    return pytest.approx(v, abs=0.1)


def test_unknown_movement_opcode_is_learned():
    from aion2_helper.protocol import EntityMove, encode_varint

    stream = b""
    for i in range(20):
        stream += frame(b"\x2e\x37" + encode_varint(500 + i) + b"\x03" + struct.pack("<fff", 1000.0 + i, -2000.0, 300.0) + b"\x01\x02")
    det = CubeDetector(Config(None))
    events, _ = ServerStreamParser(DEFAULT_OPCODES, det.diag).consume(stream)
    moves = [e for e in events if isinstance(e, EntityMove)]
    assert det.diag.learned_moves == {"2e 37": 1}
    assert len(moves) >= 7 and moves[-1].pos == (1019.0, -2000.0, 300.0)
    assert b"\x2e\x37" not in DEFAULT_OPCODES.moves  # learned per parser, not globally


def test_noise_opcode_is_not_learned_as_movement():
    import os

    from aion2_helper.protocol import encode_varint

    stream = b"".join(frame(b"\x2a\x37" + encode_varint(300 + i) + os.urandom(20)) for i in range(60))
    det = CubeDetector(Config(None))
    ServerStreamParser(DEFAULT_OPCODES, det.diag).consume(stream)
    assert det.diag.learned_moves == {}


# Third live session: 34 36 objects. 1500000 sat on an odyle gathering node, to 0.0 m.
LIVE3_OBJECTS = [
    "3d 34 36 ad 87 02 04 00 b9 52 12 00 ed aa 38 c7 1c 4b fe 47 00 04 9d 46 00 00 00 00 00 ac ad 43 01 01 01 2b 3c 5f 39 a1 01 00 00 00 00 00 00 00 00 00 00 8f 9c 3c 60 07",
    "3d 34 36 bd b1 02 04 00 60 e3 16 00 00 bf 62 c7 00 ea f9 47 df 6f 9d 46 00 00 00 00 00 00 00 00 01 01 01 84 c1 dd 3a a1 01 00 00 00 00 00 00 00 00 00 00 e4 31 83 b7 07",
]


def test_live_object_spawns_are_read():
    import pytest

    events, _ = ServerStreamParser().consume(b"".join(_live(h) for h in LIVE3_OBJECTS))
    spawns = [e for e in events if isinstance(e, Spawn)]
    assert [(s.entity_id, s.code) for s in spawns] == [(33709, 1200825), (39101, 1500000)]
    assert spawns[1].pos == pytest.approx((-58047.0, 127956.0, 20151.9), abs=0.1)


def test_other_players_are_never_taken_for_you():
    """Flags 05 is no sign of you: other players walking near you carry it too (seen live)."""
    from aion2_helper.protocol import EntityMove, SelfStats, ZoneChange

    det, clock = _det()
    det.handle(ZoneChange((1000.0, 1000.0, 0.0)))
    det.handle(SelfStats(2348))
    for i in range(20):
        clock.t += 0.1
        det.handle(EntityMove(777, (1000.0 + i * 50, 1000.0, 0.0), flags=0x05))
        det.handle(EntityMove(778, (1000.0, 1000.0 + i * 50, 0.0), flags=0x05))
    assert det.self_id == 2348
    clock.t += 0.3
    det.handle(EntityMove(2348, (1100.0, 1000.0, 0.0)))
    clock.t += 0.3
    det.handle(EntityMove(2348, (1220.0, 1000.0, 0.0)))
    clock.t += 0.2
    v = det.snapshot().velocity
    assert v is not None and v[0] > 50 and abs(v[1]) < 1  # moving east
    clock.t += 0.3
    det.handle(EntityMove(2348, (1400.0, 1000.0, 0.0)))
    assert det.snapshot().heading_deg == pytest_approx(90.0)


# Fourth live session: you are ae 3a (7470), your pet a8 ef 01 (30632) walks beside you.
LIVE4_TELEPORT = "1e 23 36 00 00 00 00 00 00 ae 6a 26 45 36 c6 aa 47 00 ee 1e 47 04 00 00 00 95 30"
LIVE4_SELF = [
    "18 1a 37 ae 3a 00 00 6c 7b 27 45 8e a3 ad 47 00 71 1f 47 6b 6e",
    "24 29 37 ae 3a 03 03 5e 0c 7a 45 43 3d ad 47 2a e9 1f 47 6c a3 c0 c4 14 c0 51 40 b7 0b d7 43 de 7f",
]
LIVE4_PET = "1b 1b 37 a8 ef 01 04 57 ba 40 45 e0 25 ab 47 00 11 1f 47 b7 de b7 de 01"


def test_live_29_37_carries_your_velocity():
    import pytest

    events, _ = ServerStreamParser().consume(_live(LIVE4_SELF[1]))
    (mv,) = events
    assert mv.entity_id == 7470
    assert mv.pos == pytest.approx((4000.8, 88698.5, 40937.2), abs=0.2)
    assert mv.velocity == pytest.approx((-1541.1, 3.3, 430.1), abs=0.2)


def test_live_pet_spawned_in_the_zone_bundle_is_not_you():
    from aion2_helper.protocol import SelfStats

    det, clock = _det()
    det.handle(Spawn(30632, 0x5F, 2_990_000, (3000.0, 87500.0, 40700.0)))  # pet, before the teleport
    det.handle(SelfStats(7470))
    self_1b = "1a 1b 37 ae 3a 05 03 ca a3 40 45 f1 66 ad 47 3f 7a 1f 47 9f 7c 9f 7c"
    stream = (_live(LIVE4_TELEPORT) + _live(LIVE4_PET) + _live(LIVE4_SELF[0]) + _live(LIVE4_PET) + _live(self_1b)
              + _live(LIVE4_SELF[1]))  # fmt: skip
    for e in ServerStreamParser().consume(stream)[0]:
        clock.t += 0.4
        det.handle(e)
    assert det.self_id == 7470 and det.self_source == "servidor"
    snap = det.snapshot()
    assert snap.velocity == (pytest_approx(-1541.1), pytest_approx(3.3))  # straight from the record


# Seventh live session: you are dd 3d (7901), 12.8 m from the teleport, flags 05.
LIVE5_TELEPORT = "1e 23 36 00 00 00 00 00 00 ae 6a 26 45 36 86 aa 47 00 f1 1e 47 04 00 00 00 e0 35"
LIVE5_SELF = "1a 1c 37 dd 3d 05 03 89 c6 58 45 91 75 ac 47 8f 8b 1f 47 7a f1 7a f1"
LIVE5_PET = "19 1a 37 a8 ef 01 00 00 49 41 40 45 78 25 ab 47 00 11 1f 47 21 6e"


def test_live_your_own_player_spawn_does_not_hide_you():
    from aion2_helper.protocol import PlayerSpawn, SelfStats

    det, clock = _det()
    det.handle(PlayerSpawn(7901))
    det.handle(Spawn(30632, 0x5F, 2_990_000, (3080.0, 87600.0, 40700.0)))  # the pet
    det.handle(SelfStats(7901))
    for e in ServerStreamParser().consume(_live(LIVE5_TELEPORT) + _live(LIVE5_PET) + _live(LIVE5_SELF))[0]:
        clock.t += 0.3
        det.handle(e)
    assert det.self_id == 7901 and det.self_source == "servidor"


# Eighth live session: a monster (d4 92 02) walking: 1b, 1c, 1c every 1.99 m, then 1a 0.57 m on.
LIVE6_WALK = [
    "1b 1b 37 d4 92 02 04 1c a9 22 47 28 bd 90 46 00 31 14 47 7a 92 7a 92 01",
    "1b 1c 37 d4 92 02 04 48 f6 21 47 d4 0e 90 46 00 44 14 47 7b 92 7b 92 01",
    "1b 1c 37 d4 92 02 04 5f 43 21 47 6c 60 8f 46 00 61 14 47 7b 92 7b 92 01",
    "19 1a 37 d4 92 02 00 00 c4 0f 21 47 1a 2e 8f 46 00 61 14 47 7c 92",
]


def test_live_yaw_is_the_walking_direction():
    import math

    events, _ = ServerStreamParser().consume(b"".join(_live(h) for h in LIVE6_WALK))
    assert [e.phase for e in events] == ["start", "move", "move", "stop"]
    for a, b in zip(events, events[1:]):
        walked = math.degrees(math.atan2(b.pos[1] - a.pos[1], b.pos[0] - a.pos[0])) % 360
        assert abs(walked - a.yaw) < 0.5  # 206.0


def test_your_walk_is_carried_along_your_yaw_until_you_stop():
    import math

    from aion2_helper.protocol import EntityMove

    det, clock = _det()
    det.self_id = 949
    evs = ServerStreamParser().consume(b"".join(_live(h) for h in LIVE6_WALK))[0]
    for e in evs[:3]:
        clock.t += 1.0
        det.handle(EntityMove(949, e.pos, None, 0x05, e.yaw, e.phase))
    snap = det.snapshot()
    vx, vy = snap.velocity
    assert math.hypot(vx, vy) == pytest_approx(199.0) or abs(math.hypot(vx, vy) - 199) < 2  # 1.99 m per second
    assert abs(math.degrees(math.atan2(vy, vx)) % 360 - 206.0) < 0.5
    assert snap.heading_deg == pytest_approx(bearing_of(206.0))
    clock.t += 0.3
    e = evs[3]
    det.handle(EntityMove(949, e.pos, None, None, e.yaw, e.phase))
    assert det.snapshot().velocity is None  # stopped: nothing to carry forward


def bearing_of(yaw: float) -> float:
    import math

    from aion2_helper.detector import bearing

    a = math.radians(yaw)
    return bearing((0.0, 0.0, 0.0), (math.cos(a), math.sin(a), 0.0))


def test_packets_about_you_are_traced(tmp_path):
    from aion2_helper.protocol import encode_varint

    det = CubeDetector(Config(None))
    det.diag.trace_path = tmp_path / "selftrace.txt"
    parser = ServerStreamParser(DEFAULT_OPCODES, det.diag)
    parser.watch_id = 949
    parser.consume(frame(b"\x1d\x37" + encode_varint(949) + b"\x2e\xef\xf8\x0a\x7b\x92\x7b\x92\x01")
                   + frame(b"\x1d\x37" + encode_varint(950) + b"\x2e\x00")
                   + frame(b"\x45\x36" + encode_varint(949) + b"\x05Name!"))  # names are never traced
    lines = (tmp_path / "selftrace.txt").read_text().splitlines()
    assert len(lines) == 1 and " 1d 37 " in lines[0] and "b5 07" in lines[0]
    assert dict(det.diag.self_opcodes) == {"1d 37": 1}


def test_impossible_jumps_of_yours_are_ignored():
    from aion2_helper.protocol import EntityMove

    det, clock = _det()
    det.self_id = 650
    det.handle(EntityMove(650, (34625.0, 33763.0, 30041.0), flags=0x05))
    clock.t += 0.5
    det.handle(EntityMove(650, (90000.0, -50000.0, 30041.0), flags=0x05))  # 1 km in half a second: a misread
    assert det.tracker.position == (34625.0, 33763.0, 30041.0)
    clock.t += 0.5
    det.handle(EntityMove(650, (34925.0, 33763.0, 30041.0), learned=True))  # 3 m: fine
    assert det.tracker.position == (34925.0, 33763.0, 30041.0)
    assert det.diag.counts["moves_rejected"] == 1


def test_trace_exists_from_the_start_and_keeps_movement_while_you_are_unknown(tmp_path):
    from aion2_helper.protocol import encode_varint

    det = CubeDetector(Config(None))
    det.diag.start_trace(tmp_path / "logs" / "selftrace.txt")
    assert (tmp_path / "logs" / "selftrace.txt").read_text().startswith("# Aion2 Helper")
    parser = ServerStreamParser(DEFAULT_OPCODES, det.diag)  # watch_id None: you are not known yet
    parser.consume(frame(b"\x1d\x37" + encode_varint(949) + b"\x2e\x08\x05\x01\x95\x18\x95\x18\x01")
                   + frame(b"\x00\x8d\x01\x02\x03")  # not movement: left out
                   + frame(b"\x45\x36" + encode_varint(949) + b"\x05Name!")  # names: never
                   + _live(LIVE_TELEPORT))
    det.diag.note("tu entidad: 949 (flags 05)")
    body = (tmp_path / "logs" / "selftrace.txt").read_text().splitlines()[3:]  # after the 3-line header
    assert [l.split()[1:3] for l in body[:2]] == [["1d", "37"], ["23", "36"]]
    assert body[2].endswith("tu entidad: 949 (flags 05)") and len(body) == 3


def _replay_trace(path, self_id=None):
    import math
    from pathlib import Path

    from aion2_helper.protocol import EntityMove

    from .test_detector import Clock

    clock = Clock()
    det = CubeDetector(Config(None), clock=clock)
    if self_id is not None:
        det.self_id = self_id
    parser = ServerStreamParser()
    errors = []
    for line in Path(path).read_text().splitlines():
        if line.startswith("#"):
            continue
        p = line.split()
        t = float(p[0])
        clock.t = t
        for e in parser.consume(bytes.fromhex("".join(p[4:])))[0]:
            if isinstance(e, EntityMove) and e.entity_id == det.self_id and det._dr is not None and det._dr_vel is not None:
                est = det._dr_estimate(t)
                errors.append(math.hypot(est[0] - e.pos[0], est[1] - e.pos[1]) / 100)
            det.handle(e, t)
    return det, errors


def test_live_velocity_records_decode():
    import pytest

    from aion2_helper.protocol import EntityVelocity

    (v,) = ServerStreamParser().consume(_live("11 1d 37 ca 03 2f 03 d1 52 1e 50 55 50 55"))[0]
    assert isinstance(v, EntityVelocity) and v.entity_id == 458
    assert (v.vx, v.vy, v.vz) == (-470.0, 820.0, 300.0)  # 10 cm/s units
    assert v.yaw == pytest.approx(120.0, abs=0.1)
    (m,) = ServerStreamParser().consume(_live("11 1d 37 dc 90 02 26 f7 02 02 74 02 74 01"))[0]  # a monster: no mode byte
    assert (m.vx, m.vy, m.vz) == (-90.0, 20.0, None)
    (f,) = ServerStreamParser().consume(_live("1b 20 37 a1 72 0b a4 36 56 27 69 28 99 5d c4 2c d0 03 44 bf b0 32 44 01"))[0]
    assert (round(f.vx), round(f.vy), round(f.vz)) == (-886, 527, 715)  # flying, floats


def test_live_effect_on_yourself_gives_your_position():
    import pytest

    from aion2_helper.protocol import EntityMove

    rec = ("33 2b 38 ca 03 13 fe 4a b3 93 d7 0a 60 09 00 00 00 00 00 00 41 64 f5 3a a1 01 00 00 ca 03 0a c5 8e 15 01 02"
           " 5c 5c 18 47 17 04 9f 46 58 78 18 47")
    (mv,) = ServerStreamParser().consume(_live(rec))[0]
    assert isinstance(mv, EntityMove) and mv.entity_id == 458
    assert mv.pos == pytest.approx((39004.4, 20354.0, 39032.3), abs=0.1)


def test_live_walk_is_followed_between_fixes():
    """Replays a live trace: the dead-reckoned point lands near every next fix of yours."""
    from pathlib import Path

    det, errors = _replay_trace(Path(__file__).parent / "data" / "selftrace_walk.txt", self_id=458)
    assert len(errors) >= 5
    errors.sort()
    assert errors[len(errors) // 2] < 0.6  # metres, median
    assert errors[-1] < 3.0



def test_live_crowd_with_flags_05_does_not_steal_your_entity():
    """Replays a live trace (flying, with players walking around): you stay 14625 throughout."""
    from pathlib import Path

    det, _ = _replay_trace(Path(__file__).parent / "data" / "selftrace_flight.txt")
    assert det.self_id == 14625


def test_flying_records_carry_your_velocity():
    import pytest

    from aion2_helper.protocol import EntityVelocity, SelfStats

    rec = "1b 2a 37 a1 72 1f 03 82 6b 3e fd ad 8f c4 09 c9 71 44 32 b6 09 44 83 63"
    (v,) = ServerStreamParser().consume(_live(rec))[0]
    assert isinstance(v, EntityVelocity) and v.entity_id == 14625
    assert (v.vx, v.vy, v.vz) == (pytest.approx(-1149.4, abs=0.1), pytest.approx(967.1, abs=0.1), pytest.approx(550.8, abs=0.1))
    (st,) = ServerStreamParser().consume(frame(b"\x4a\x36\xa1\x72\x01\x05\x00\x10\x00\x00\x00" + b"\x00" * 8))[0]
    assert st == SelfStats(14625)
