"""Command line: ``python -m aion2_helper [run|replay|analyze|import-spots|ifaces]``."""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import Config, default_path


def _detector(cfg: Config, log: Path | None, overlay_ref: list):
    from .detector import CubeDetector
    from .spots import SpotStore

    spots = SpotStore(cfg.path.parent / "spots.json" if cfg.path else None)
    return CubeDetector(
        cfg,
        on_new_cube=lambda e: overlay_ref[0].alert() if overlay_ref else print(f"¡Hidden Cube! {e}"),
        discovery_log=log,
        spots=spots,
    )


def _log_path(args, cfg: Config) -> Path | None:
    if args.log is None:
        return None
    if args.log != "auto":
        return Path(args.log)
    base = (cfg.path.parent if cfg.path else Path.cwd()) / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"discovery-{datetime.now():%Y%m%d-%H%M%S}.jsonl"


def cmd_run(args) -> int:
    from .capture import Dispatcher, LiveCapture, PcapWriter, replay
    from .protocol import Opcodes

    cfg = Config(None) if args.demo else Config.load(Path(args.config) if args.config else None)
    overlay_ref: list = []
    detector = _detector(cfg, _log_path(args, cfg), overlay_ref)
    stop = threading.Event()
    recorder = PcapWriter(Path(args.record)) if args.record else None
    capture = None

    if args.demo:
        from . import demo

        threading.Thread(target=demo.run, args=(detector, stop), daemon=True).start()
    else:
        dispatcher = Dispatcher(detector, Opcodes.from_config(cfg["opcodes"]), recorder)
        if args.replay:
            def go():
                n = replay(Path(args.replay), dispatcher, realtime=not args.fast, stop=stop)
                detector.status = f"Reproducción terminada ({n} segmentos)"
                if args.headless:
                    stop.set()

            threading.Thread(target=go, daemon=True).start()
        else:
            iface = args.iface
            if iface == "all":
                from scapy.all import conf

                iface = [i.name for i in conf.ifaces.values()]
            capture = LiveCapture(dispatcher, iface, cfg["game_process"])
            try:
                capture.start()
            except Exception as exc:
                print(f"No se pudo iniciar la captura: {exc}\n¿Está instalado Npcap (modo WinPcap) y ejecutas como administrador?")
                return 1

    def shutdown():
        stop.set()
        if capture:
            capture.stop()
        if recorder:
            recorder.close()
        detector.close()

    if args.headless:
        try:
            last = None
            while not stop.wait(1.0):
                snap = detector.snapshot()
                line = f"{snap.status} | entidades {snap.entity_count} | cubos {len(snap.cubes)}"
                if line != last:
                    print(line)
                    last = line
        except KeyboardInterrupt:
            pass
        snap = detector.snapshot()
        for v in snap.cubes:
            print("CUBO", v.entity, v.distance_m)
        shutdown()
        return 0

    from .overlay import Overlay, start_hotkeys

    overlay = Overlay(detector, on_close=shutdown)
    overlay.discovery = args.discovery
    overlay_ref.append(overlay)
    hk = start_hotkeys(overlay, cfg["hotkeys"])
    if hk is None:
        print("Atajos globales desactivados (instala 'pynput'). Clic derecho en el overlay para cerrar.")
    overlay.run()
    return 0


def cmd_analyze(args) -> int:
    from .analyze import main

    main(Path(args.log), args.top)
    return 0


def cmd_import(args) -> int:
    from .spots import SpotStore

    cfg = Config.load(Path(args.config) if args.config else None)
    store = SpotStore(cfg.path.parent / "spots.json")
    added = store.merge_file(Path(args.file))
    print(f"{added} spots nuevos; {len(store.spots)} en total ({store.path})")
    return 0


def cmd_ifaces(args) -> int:
    from .capture import list_interfaces

    for line in list_interfaces():
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aion2_helper", description="Overlay de Hidden Cubes para AION 2 (uso personal).")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help=f"ruta de config.json (por defecto {default_path()})")
    sub = p.add_subparsers(dest="cmd")

    def run_args(sp):
        sp.add_argument("--iface", help="interfaz de red (ver 'ifaces'); 'all' = todas")
        sp.add_argument("--log", nargs="?", const="auto", help="guarda un registro de descubrimiento (JSONL)")
        sp.add_argument("--record", help="graba el tráfico del juego en un .pcap")
        sp.add_argument("--discovery", action="store_true", help="muestra también las entidades cercanas")
        sp.add_argument("--headless", action="store_true", help="sin overlay; imprime en consola")
        sp.add_argument("--demo", action="store_true", help="datos inventados para ver el overlay")
        sp.add_argument("--replay", help="reproduce un .pcap en lugar de capturar")
        sp.add_argument("--fast", action="store_true", help="con --replay: sin esperar al tiempo real")

    run_args(p)
    run_args(sub.add_parser("run", help="captura en directo con overlay (por defecto)"))
    rp = sub.add_parser("replay", help="reproduce un .pcap")
    rp.add_argument("pcap")
    run_args(rp)
    an = sub.add_parser("analyze", help="busca el código del cubo en un registro de descubrimiento")
    an.add_argument("log")
    an.add_argument("--top", type=int, default=10)
    im = sub.add_parser("import-spots", help="importa spots (CSV x,y,z o JSON) en coordenadas del mundo")
    im.add_argument("file")
    sub.add_parser("ifaces", help="lista las interfaces de red")

    args = p.parse_args(argv)
    if args.cmd == "replay":
        args.replay = args.pcap
    if args.cmd in (None, "run", "replay"):
        return cmd_run(args)
    return {"analyze": cmd_analyze, "import-spots": cmd_import, "ifaces": cmd_ifaces}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
