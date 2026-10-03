"""Command line:  python -m ledwetter <command>

  update   build or update the local database (first run: the entire available history)
  export   write a period from all sources as a homogeneous CSV (no database needed)
  serve    open the visualisation in the browser at http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

from .export import collect, write_csv
from .http import HttpClient
from .ingest import Ingestor
from .model import Period
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

    args = ap.parse_args(argv)
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

    http = HttpClient(settings.get("user_agent", "ledwetter/0.1"), settings.get("timeout_seconds", 120),
                      settings.get("request_delay_seconds", 0.0))
    names = args.sources.split(",") if args.sources else None
    sources = build_sources(cfg, http, names)

    if args.cmd == "export":
        period = Period.from_args(args.start, args.end)
        log.info("Zeitraum: %s", period)
        n = write_csv(collect(sources, period), args.out)
        log.info("%d Zeilen geschrieben: %s", n, args.out)
    elif args.cmd == "update":
        since = Period.parse_bound(args.since, is_end=False) if args.since else None
        counts = Ingestor(MeasurementStore(db_path), sources).update(since)
        log.info("Fertig: %d Werte in %s", sum(counts.values()), db_path)


if __name__ == "__main__":
    main()
