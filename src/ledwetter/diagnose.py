"""One-command diagnostics: record a short real run and summarise what ended up in the database.

    python -m ledwetter diagnose            -> diagnose/ and diagnose.zip

Steps: station discovery for the canton, a short update (default: since midnight local time) into a
separate database, a summary of every stored series (count, time range, min/max, unit, aggregation) and
the log, all packed into one ZIP file. The recording can be replayed without network access with
'--replay diagnose' to reproduce the run exactly.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .discover import StationDiscovery
from .ingest import Ingestor
from .model import LOCAL_TZ, Clock, utcnow
from .sources import build_sources
from .storage import MeasurementStore

log = logging.getLogger(__name__)


def summarise(store: MeasurementStore) -> pd.DataFrame:
    """One row per stored series with count, time range and value range."""
    rows = []
    for s in store.series():
        n, vmin, vmax = store.conn.execute("SELECT COUNT(*), MIN(value), MAX(value) FROM measurements WHERE series = ?",
                                           (s["id"],)).fetchone()
        rows.append({"source": s["source"], "station_id": s["station_id"], "station": s["station_name"],
                     "canton": s["canton"],
                     "parameter": s["parameter"], "description": s["description"], "unit": s["unit"],
                     "agg": s["agg"], "quantity": s["quantity"], "time_ref": s["time_ref"],
                     "interval_min": s["interval_min"], "count": n, "min": vmin, "max": vmax,
                     "first_utc": pd.Timestamp(s["first"], unit="s", tz="UTC") if s["first"] else None,
                     "last_utc": pd.Timestamp(s["last"], unit="s", tz="UTC") if s["last"] else None})
    return pd.DataFrame(rows)


def run(http, settings: dict, out_dir: str, cantons="ZH", since: datetime | None = None,
        clock: Clock = utcnow) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(out / "diagnose.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        discovery = StationDiscovery(http, cantons, clock)
        log.info("Diagnostics: station discovery for %s", ", ".join(discovery.cantons))
        cfg = discovery.write(str(out / f"stations_{'_'.join(discovery.cantons).lower()}.yaml"), settings)
        if since is None:
            since = pd.Timestamp(clock()).tz_convert(LOCAL_TZ).normalize().tz_convert("UTC").to_pydatetime()
        db = out / "diagnose.db"
        if db.exists():
            db.unlink()
        store = MeasurementStore(db)
        log.info("Diagnostics: update since %s", since.isoformat())
        counts = Ingestor(store, build_sources(cfg, http, clock=clock), clock).update(since)
        summary = summarise(store)
        summary.to_csv(out / "summary.csv", index=False)
        log.info("Diagnostics: %d series, %d values", len(summary), sum(counts.values()))
        store.close()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    archive = shutil.make_archive(str(out), "zip", root_dir=out.parent, base_dir=out.name)
    log.info("Diagnostics written: %s", archive)
    return Path(archive)
