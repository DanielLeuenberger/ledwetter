"""Local measurement database (SQLite).

Schema (normalised, compact):
  stations(id, source, station_id, name)
  series(id, station, parameter, unit, medium, time_ref, interval_min)   -- one time series
  measurements(series, t, value)  -- t = Unix seconds UTC (measurement time), WITHOUT ROWID

Aggregation (hour/day) is done in SQL. Bins are labelled with their start (UTC).
Values with time_ref = interval_end are assigned to the interval in which they were measured
(the 14:00 value for 13:50–14:00 belongs to the hour 13:00–14:00).
"""
from __future__ import annotations

import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

AGG_SECONDS = {"hour": 3600, "day": 86400}
SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    station_id TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (source, station_id)
);
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    station INTEGER NOT NULL REFERENCES stations(id),
    parameter TEXT NOT NULL,
    unit TEXT NOT NULL,
    medium TEXT NOT NULL,
    time_ref TEXT NOT NULL,
    interval_min INTEGER NOT NULL,          -- -1 = unknown
    UNIQUE (station, parameter, time_ref, interval_min)
);
CREATE TABLE IF NOT EXISTS measurements (
    series INTEGER NOT NULL REFERENCES series(id),
    t INTEGER NOT NULL,                     -- measurement time, Unix seconds UTC
    value REAL NOT NULL,
    PRIMARY KEY (series, t)
) WITHOUT ROWID;
"""
UPSERT = ("INSERT INTO measurements (series, t, value) VALUES (?, ?, ?) "
          "ON CONFLICT (series, t) DO UPDATE SET value = excluded.value")
SERIES_KEY = ["source", "station_id", "station_name", "parameter", "unit", "medium", "time_ref", "interval_min"]


def to_epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class MeasurementStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self._series_cache: dict = {}

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- Writing
    def upsert(self, df: pd.DataFrame) -> int:
        """Write a canonical frame; existing (series, t) rows are overwritten."""
        if df is None or df.empty:
            return 0
        df = df.copy()
        df["interval_min"] = df["interval_min"].fillna(-1).astype(int)
        df["t"] = (df["time_utc"].astype("datetime64[ns, UTC]").astype("int64") // 10**9).astype(int)
        n = 0
        with self.lock, self.conn:
            for key, g in df.groupby(SERIES_KEY, sort=False):
                sid = self._series_id(*key)
                self.conn.executemany(UPSERT, zip([sid] * len(g), g["t"].tolist(), g["value"].astype(float).tolist()))
                n += len(g)
        return n

    def _series_id(self, source, station_id, station_name, parameter, unit, medium, time_ref, interval_min) -> int:
        key = (source, station_id, parameter, time_ref, int(interval_min))
        if key in self._series_cache:
            return self._series_cache[key]
        self.conn.execute("INSERT INTO stations (source, station_id, name) VALUES (?, ?, ?) "
                          "ON CONFLICT (source, station_id) DO UPDATE SET name = excluded.name",
                          (source, station_id, station_name))
        st = self.conn.execute("SELECT id FROM stations WHERE source = ? AND station_id = ?",
                               (source, station_id)).fetchone()[0]
        self.conn.execute("INSERT OR IGNORE INTO series (station, parameter, unit, medium, time_ref, interval_min) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (st, parameter, unit, medium, time_ref, int(interval_min)))
        sid = self.conn.execute("SELECT id FROM series WHERE station = ? AND parameter = ? AND time_ref = ? "
                                "AND interval_min = ?", (st, parameter, time_ref, int(interval_min))).fetchone()[0]
        self._series_cache[key] = sid
        return sid

    # ---------------------------------------------------------------- Reading
    def last_time(self, source: str, station_id: str) -> Optional[datetime]:
        """Latest measurement time of a station across all its series (for incremental updates)."""
        with self.lock:
            ids = [r[0] for r in self.conn.execute(
                "SELECT se.id FROM series se JOIN stations st ON st.id = se.station "
                "WHERE st.source = ? AND st.station_id = ?", (source, station_id))]
            ts = [self.conn.execute("SELECT MAX(t) FROM measurements WHERE series = ?", (i,)).fetchone()[0] for i in ids]
        ts = [t for t in ts if t is not None]
        return datetime.fromtimestamp(max(ts), tz=timezone.utc) if ts else None

    def series(self) -> list:
        """All time series with station and time range (first/last as Unix seconds)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT se.id, st.source, st.station_id, st.name, se.parameter, se.unit, se.medium, "
                "se.time_ref, se.interval_min FROM series se JOIN stations st ON st.id = se.station "
                "ORDER BY st.source, st.name, se.parameter").fetchall()
            out = []
            for r in rows:
                first, last = self.conn.execute(
                    "SELECT (SELECT MIN(t) FROM measurements WHERE series = ?), "
                    "(SELECT MAX(t) FROM measurements WHERE series = ?)", (r[0], r[0])).fetchone()
                out.append(dict(id=r[0], source=r[1], station_id=r[2], station_name=r[3], parameter=r[4],
                                unit=r[5], medium=r[6], time_ref=r[7],
                                interval_min=None if r[8] < 0 else r[8], first=first, last=last))
        return out

    def series_info(self, series_id: int) -> Optional[dict]:
        return next((s for s in self.series() if s["id"] == series_id), None)

    def count(self, series_id: int, start: int, end: int) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM measurements WHERE series = ? AND t >= ? AND t <= ?",
                                     (series_id, start, end)).fetchone()[0]

    def raw(self, series_id: int, start: int, end: int) -> dict:
        with self.lock:
            rows = self.conn.execute("SELECT t, value FROM measurements WHERE series = ? AND t >= ? AND t <= ? "
                                     "ORDER BY t", (series_id, start, end)).fetchall()
        return {"t": [r[0] for r in rows], "mean": [r[1] for r in rows], "min": None, "max": None, "n": None}

    def aggregate(self, series_id: int, start: int, end: int, agg: str, min_coverage: float = 0.75) -> dict:
        """Mean, min, max and count per hour/day (UTC). Bins with too little coverage are dropped
        when the expected number of values is known (interval means)."""
        w = AGG_SECONDS[agg]
        info = self.series_info(series_id)
        if info is None:
            raise KeyError(series_id)
        off = -1 if info["time_ref"] == "interval_end" else 0
        a, b = (start // w) * w, -(-end // w) * w
        with self.lock:
            rows = self.conn.execute(
                "SELECT ((t + ?) / ?) * ? AS bin, AVG(value), MIN(value), MAX(value), COUNT(*) "
                "FROM measurements WHERE series = ? AND t >= ? AND t < ? GROUP BY bin ORDER BY bin",
                (off, w, w, series_id, a - off, b - off)).fetchall()
        interval = info["interval_min"]
        expected = w // (interval * 60) if interval and info["time_ref"] != "instant" else None
        need = math.ceil(expected * min_coverage) if expected else 0
        rows = [r for r in rows if r[4] >= need]
        return {"t": [r[0] for r in rows], "mean": [round(r[1], 3) for r in rows],
                "min": [r[2] for r in rows], "max": [r[3] for r in rows], "n": [r[4] for r in rows],
                "expected": expected}
