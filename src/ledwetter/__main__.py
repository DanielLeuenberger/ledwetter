"""Command line:  python -m ledwetter <command>

  update   build or update the local database (first run: the entire available history)
  export   write a period from all sources as a homogeneous CSV (no database needed)
  serve    open the visualisation in the browser at http://127.0.0.1:8000
  stations list all stations of the sources within a canton and write them as a configuration file
  diagnose record a short real run of all sources and pack responses, log and summary into a ZIP

Diagnostics: '--record DIR' stores every response of the sources; '--replay DIR' answers all requests
from such a recording without network access (the clock is set to the time of the recording).
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

from .discover import StationDiscovery
from .geo import parse_cantons
from .export import collect, write_csv
import json
import platform
from datetime import datetime, timezone

import pandas as pd

from . import __version__
from .http import HttpClient, RecordingHttpClient, ReplayHttpClient
from .ingest import Ingestor
from . import diagnose
from .model import Period, utcnow
from .server import make_server
from .sources import REGISTRY, build_sources
from .storage import MeasurementStore

log = logging.getLogger("ledwetter")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="ledwetter", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/stations.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--record", metavar="DIR", help="store every response of the sources in DIR (diagnostics)")
    ap.add_argument("--replay", metavar="DIR", help="answer all requests from a recording in DIR (no network)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("update", help="build or update the database")
    up.add_argument("--db", help="SQLite file (default: settings.db_file)")
    up.add_argument("--since", help="reload from this time (local time) instead of the latest measurement")
    up.add_argument("--sources", help=f"comma-separated, choose from: {', '.join(REGISTRY)}")

    ex = sub.add_parser("export", help="export a period as CSV")
    ex.add_argument("--start", help="local time, e.g. 2026-10-01 or '2026-10-01 06:00' (default: end - 24 h)")
    ex.add_argument("--end", help="local time; a date only includes the whole day (default: now)")
    ex.add_argument("--out", default="wetter_export.csv")
    ex.add_argument("--sources", help=f"comma-separated, choose from: {', '.join(REGISTRY)}")

    sv = sub.add_parser("serve", help="start the visualisation")
    sv.add_argument("--db")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)

    st = sub.add_parser("stations", help="discover all stations within one or more cantons")
    st.add_argument("--canton", default="ZH", help="canton code(s), comma-separated, e.g. ZH or ZH,AG,SH")
    st.add_argument("--out", help="output file (default: config/stations_<cantons>.yaml)")

    dg = sub.add_parser("diagnose", help="record a short real run for checking the import")
    dg.add_argument("--canton", default="ZH", help="canton code(s), comma-separated")
    dg.add_argument("--out", default="diagnose")
    dg.add_argument("--since", help="start of the test run (local time; default: today 00:00)")

    args = ap.parse_args(argv)
    if getattr(args, "canton", None):
        try:
            args.cantons = parse_cantons(args.canton)
        except ValueError as e:
            raise SystemExit(str(e))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    settings = cfg.get("settings", {})
    db_path = getattr(args, "db", None) or settings.get("db_file", "data/ledwetter.db")

    if args.cmd == "serve":
        if not Path(db_path).exists():
            raise SystemExit(f"Datenbank {db_path} fehlt. Zuerst 'python -m ledwetter update' ausführen.")
        httpd = make_server(MeasurementStore(db_path), args.host, args.port)
        print(f"Visualisierung: http://{args.host}:{args.port}  (Beenden mit Ctrl+C)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    http_args = (settings.get("user_agent", "ledwetter/0.1"), settings.get("timeout_seconds", 120),
                 settings.get("request_delay_seconds", 0.0))
    if args.cmd == "diagnose" and not args.replay:
        args.record = args.record or args.out
    if args.record and args.replay:
        raise SystemExit("--record and --replay cannot be combined.")
    clock = utcnow
    if args.replay:
        http = ReplayHttpClient(args.replay)
        recorded = recording_time(args.replay)
        if recorded:
            clock = lambda: recorded
            log.info("Replay: clock set to %s", recorded.isoformat())
    elif args.record:
        http = RecordingHttpClient(args.record, *http_args)
        started = write_run_info(args, cfg)
        clock = lambda: started   # frozen, so that a replay produces exactly the same requests
    else:
        http = HttpClient(*http_args)

    if args.cmd == "diagnose":
        since = Period.parse_bound(args.since, is_end=False) if args.since else None
        archive = diagnose.run(http, settings, args.out, args.cantons, since, clock)
        print(f"Fertig. Bitte diese Datei hochladen: {archive}")
        return

    if args.cmd == "stations":
        out = args.out or f"config/stations_{'_'.join(args.cantons).lower()}.yaml"
        found = StationDiscovery(http, args.cantons, clock).write(out, settings)
        for name, section in found.items():
            if name != "settings":
                log.info("%-12s %3d stations in total", name, len(section["stations"]))
        log.info("Written: %s", out)
        return
    names = args.sources.split(",") if args.sources else None
    sources = build_sources(cfg, http, names, clock=clock)

    if args.cmd == "export":
        period = Period.from_args(args.start, args.end, now=clock())
        log.info("Zeitraum: %s", period)
        n = write_csv(collect(sources, period), args.out)
        log.info("%d Zeilen geschrieben: %s", n, args.out)
    elif args.cmd == "update":
        since = Period.parse_bound(args.since, is_end=False) if args.since else None
        counts = Ingestor(MeasurementStore(db_path), sources, clock).update(since)
        log.info("Fertig: %d Werte in %s", sum(counts.values()), db_path)


def recording_time(directory: str):
    """Start time of a recording (from run_info.json), used as the clock when replaying."""
    path = Path(directory) / "run_info.json"
    if not path.exists():
        return None
    return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["started_utc"])


def write_run_info(args, cfg: dict) -> None:
    """Context of a recording: command, versions, time and the configuration used."""
    info = {"command": vars(args), "started_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "ledwetter": __version__, "python": platform.python_version(), "pandas": pd.__version__,
            "platform": platform.platform(), "config": cfg}
    path = Path(args.record) / "run_info.json"
    path.write_text(json.dumps(info, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return datetime.fromisoformat(info["started_utc"])


if __name__ == "__main__":
    main()
