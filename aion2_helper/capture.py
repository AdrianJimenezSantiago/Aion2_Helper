"""Passive capture: find the game connection, reassemble both directions, feed the parsers.

Nothing is ever sent to the game or its servers, and the game's memory is never
touched; this only reads packets the network card already received (Npcap on
Windows), the same way the open-source DPS meters do.
"""

from __future__ import annotations

import queue
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .detector import CubeDetector
from .protocol import Opcodes, ServerStreamParser

MAX_BUFFER_BYTES = 2 * 1024 * 1024
MAX_HELD_SEGMENTS = 64
MAX_HOLD_S = 0.5
SIGNATURE_LOCK_HITS = 12
SIGNATURE_WINDOW_S = 3.0
STALE_CONNECTION_S = 120.0


@dataclass
class Segment:
    """One TCP segment with a payload."""

    t: float
    src: str
    sport: int
    dst: str
    dport: int
    seq: int
    data: bytes


class TcpStream:
    """Reassembles one direction of a TCP connection and hands complete bytes to ``consume``."""

    def __init__(self, consume: Callable[[bytes, float], int]):
        self._consume = consume
        self._buf = bytearray()
        self._next: int | None = None
        self._held: dict[int, bytes] = {}
        self._held_since = 0.0

    def add(self, seq: int, data: bytes, t: float) -> None:
        if self._next is None:
            self._append(data)
            self._next = (seq + len(data)) & 0xFFFFFFFF
        else:
            diff = _seq_diff(seq, self._next)
            if diff > 0:
                if not self._held:
                    self._held_since = t
                self._held[seq] = data
                if len(self._held) > MAX_HELD_SEGMENTS or t - self._held_since > MAX_HOLD_S:
                    self._next = min(self._held, key=lambda s: _seq_diff(s, self._next))
            else:
                skip = -diff
                if skip < len(data):
                    self._append(data[skip:])
                    self._next = (self._next + len(data) - skip) & 0xFFFFFFFF
            self._drain()
        self._parse(t)

    def _drain(self) -> None:
        while self._held and self._next is not None:
            seq = min(self._held, key=lambda s: _seq_diff(s, self._next))
            diff = _seq_diff(seq, self._next)
            if diff > 0:
                return
            data = self._held.pop(seq)
            skip = -diff
            if skip < len(data):
                self._append(data[skip:])
                self._next = (self._next + len(data) - skip) & 0xFFFFFFFF

    def _append(self, data: bytes) -> None:
        if len(self._buf) + len(data) > MAX_BUFFER_BYTES:
            self._buf.clear()  # runaway: start over and let the framing resync
        self._buf += data

    def _parse(self, t: float) -> None:
        while self._buf:
            consumed = self._consume(bytes(self._buf), t)
            if consumed <= 0:
                return
            del self._buf[:consumed]


def _seq_diff(a: int, b: int) -> int:
    d = (a - b) & 0xFFFFFFFF
    return d - 0x100000000 if d & 0x80000000 else d


class Dispatcher:
    """Locks onto the game connection and routes its two directions."""

    def __init__(self, detector: CubeDetector, opcodes: Opcodes, recorder: "PcapWriter | None" = None):
        self.detector = detector
        self.opcodes = opcodes
        self.recorder = recorder
        self.parser = ServerStreamParser(opcodes)
        self._candidates: dict[tuple, tuple[int, float]] = {}
        self._server: tuple[str, int] | None = None
        self._down: TcpStream | None = None
        self._up: TcpStream | None = None
        self._last_seen = 0.0
        self.detector.status = "Buscando la conexión del juego"

    @property
    def locked(self) -> bool:
        return self._server is not None

    def process(self, seg: Segment) -> None:
        if self._server and seg.t - self._last_seen > STALE_CONNECTION_S:
            self.reset()
        if self._server is None:
            self._search(seg)
            return
        host, port = self._server
        if (seg.src, seg.sport) == (host, port):
            self._last_seen = seg.t
            if self.recorder:
                self.recorder.write(seg)
            self._down.add(seg.seq, seg.data, seg.t)
        elif (seg.dst, seg.dport) == (host, port):
            self._last_seen = seg.t
            if self.recorder:
                self.recorder.write(seg)
            self._up.add(seg.seq, seg.data, seg.t)

    def _search(self, seg: Segment) -> None:
        d = seg.data
        if len(d) >= 3 and 0x14 <= d[0] <= 0x17 and d[1] == 0x03:
            return  # TLS: not the game stream
        if not any(sig in d for sig in self.opcodes.signatures):
            return
        key = (seg.src, seg.sport, seg.dst, seg.dport)
        hits, last = self._candidates.get(key, (0, seg.t))
        if seg.t - last > SIGNATURE_WINDOW_S:
            hits = 0
        self._candidates[key] = (hits + 1, seg.t)
        if hits + 1 >= SIGNATURE_LOCK_HITS:
            self._lock(seg)

    def _lock(self, seg: Segment) -> None:
        self._server = (seg.src, seg.sport)
        self._last_seen = seg.t
        self._candidates.clear()
        self._down = TcpStream(self._consume_down)
        self._up = TcpStream(self._consume_up)
        self.detector.status = f"Conectado al juego ({seg.src}:{seg.sport})"
        self.detector.new_connection(seg.t)
        self._down.add(seg.seq, seg.data, seg.t)

    def reset(self) -> None:
        self._server = None
        self._down = self._up = None
        self._candidates.clear()
        self.detector.status = "Buscando la conexión del juego"

    def _consume_down(self, buf: bytes, t: float) -> int:
        events, consumed = self.parser.consume(buf)
        for e in events:
            self.detector.handle(e, t)
        return consumed

    def _consume_up(self, buf: bytes, t: float) -> int:
        consumed = self.detector.tracker.feed(buf, t)
        self.detector.note_self_position()
        return consumed


# ───── pcap files (record and replay without scapy) ─────


class PcapWriter:
    """Writes segments as a minimal Ethernet/IPv4/TCP pcap, enough to replay them."""

    def __init__(self, path: Path):
        self._f = open(path, "wb")
        self._f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        self._lock = threading.Lock()

    def write(self, seg: Segment) -> None:
        import socket

        tcp = struct.pack("!HHIIBBHHH", seg.sport, seg.dport, seg.seq, 0, 5 << 4, 0x18, 65535, 0, 0)
        total = 20 + len(tcp) + len(seg.data)
        ip = struct.pack(
            "!BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, 6, 0, socket.inet_aton(seg.src), socket.inet_aton(seg.dst)
        )
        frame = b"\x00" * 12 + b"\x08\x00" + ip + tcp + seg.data
        sec = int(seg.t)
        with self._lock:
            self._f.write(struct.pack("<IIII", sec, int((seg.t - sec) * 1e6), len(frame), len(frame)))
            self._f.write(frame)
            self._f.flush()

    def close(self) -> None:
        self._f.close()


def read_pcap(path: Path) -> Iterable[Segment]:
    """Reads IPv4/TCP segments from a classic pcap (Ethernet, raw IP, loopback or Linux SLL)."""
    import socket

    with open(path, "rb") as f:
        header = f.read(24)
        magic = struct.unpack("<I", header[:4])[0]
        if magic in (0xA1B2C3D4, 0xA1B23C4D):
            e = "<"
        elif magic in (0xD4C3B2A1, 0x4D3CB2A1):
            e = ">"
        else:
            raise ValueError("not a classic pcap file (pcapng is not supported; save as .pcap)")
        nano = magic in (0xA1B23C4D, 0x4D3CB2A1)
        linktype = struct.unpack(e + "I", header[20:24])[0]
        while True:
            rec = f.read(16)
            if len(rec) < 16:
                return
            sec, frac, incl, _ = struct.unpack(e + "IIII", rec)
            pkt = f.read(incl)
            t = sec + frac / (1e9 if nano else 1e6)
            if linktype == 1:
                if len(pkt) < 14 or pkt[12:14] != b"\x08\x00":
                    continue
                ip = pkt[14:]
            elif linktype in (101, 228):
                ip = pkt
            elif linktype == 0:
                ip = pkt[4:]
            elif linktype == 113:
                ip = pkt[16:]
            else:
                continue
            seg = _parse_ipv4_tcp(ip, t, socket)
            if seg:
                yield seg


def _parse_ipv4_tcp(ip: bytes, t: float, socket) -> Segment | None:
    if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 6:
        return None
    ihl = (ip[0] & 0x0F) * 4
    total = struct.unpack("!H", ip[2:4])[0] or len(ip)
    tcp = ip[ihl:total]
    if len(tcp) < 20:
        return None
    sport, dport, seq = struct.unpack("!HHI", tcp[:8])
    off = (tcp[12] >> 4) * 4
    data = bytes(tcp[off:])
    if not data:
        return None
    return Segment(t, socket.inet_ntoa(ip[12:16]), sport, socket.inet_ntoa(ip[16:20]), dport, seq, data)


def replay(path: Path, dispatcher: Dispatcher, realtime: bool = False, stop: threading.Event | None = None) -> int:
    count = 0
    start_wall = time.time()
    first_t = None
    for seg in read_pcap(path):
        if stop is not None and stop.is_set():
            break
        if realtime:
            first_t = seg.t if first_t is None else first_t
            delay = (seg.t - first_t) - (time.time() - start_wall)
            if delay > 0:
                time.sleep(min(delay, 5))
        dispatcher.process(seg)
        count += 1
    return count


# ───── live capture ─────


def game_running(name: str) -> bool:
    """Whether the game process is up (Windows). Elsewhere, assume it is."""
    if sys.platform != "win32" or not name:
        return True
    try:
        import psutil
    except ImportError:
        return True
    target = name.lower().removesuffix(".exe")
    for p in psutil.process_iter(["name"]):
        n = (p.info.get("name") or "").lower().removesuffix(".exe")
        if n == target:
            return True
    return False


class LiveCapture:
    """Sniffs with scapy (Npcap on Windows) and processes on a worker thread."""

    def __init__(self, dispatcher: Dispatcher, iface: str | list[str] | None = None, game_process: str = ""):
        self.dispatcher = dispatcher
        self.iface = iface
        self.game_process = game_process
        self._q: "queue.Queue[Segment | None]" = queue.Queue(maxsize=20000)
        self._sniffer = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        from scapy.all import AsyncSniffer  # imported late: slow, and optional for replay/tests

        self._worker = threading.Thread(target=self._run, name="aion2-capture", daemon=True)
        self._worker.start()
        self._sniffer = AsyncSniffer(iface=self.iface, filter="tcp", prn=self._on_packet, store=False)
        self._sniffer.start()

    def _on_packet(self, pkt) -> None:
        try:
            from scapy.layers.inet import IP, TCP

            if IP not in pkt or TCP not in pkt:
                return
            tcp = pkt[TCP]
            data = bytes(tcp.payload)
            if not data:
                return
            ip = pkt[IP]
            seg = Segment(float(pkt.time), ip.src, int(tcp.sport), ip.dst, int(tcp.dport), int(tcp.seq), data)
            self._q.put_nowait(seg)
        except queue.Full:
            pass

    def _run(self) -> None:
        last_check = 0.0
        running = True
        while not self._stop.is_set():
            try:
                seg = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if seg is None:
                return
            if time.time() - last_check > (60 if running else 10):
                last_check = time.time()
                was = running
                running = game_running(self.game_process)
                if not running:
                    self.dispatcher.detector.status = f"Esperando a que arranque {self.game_process}"
                    if was:
                        self.dispatcher.reset()
                elif not was:
                    self.dispatcher.reset()
            if running:
                try:
                    self.dispatcher.process(seg)
                except Exception as exc:  # one bad packet must not kill the capture
                    self.dispatcher.detector.status = f"Error de análisis: {exc!r}"

    def stop(self) -> None:
        self._stop.set()
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:
                pass
        self._q.put(None)


def list_interfaces() -> list[str]:
    from scapy.all import conf

    return [f"{i.name}  ({i.description})" for i in conf.ifaces.values()]
