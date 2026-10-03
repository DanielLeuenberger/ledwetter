"""Local measurement database (SQLite).

Schema (normalised, compact, version 3):
  stations(id, source, station_id, name, canton, lat, lon, height_masl)
  parameters(source, code, description, unit, grp, agg, quantity)       -- parameter catalogue
  series(id, station, parameter, unit, time_ref, interval_min)          -- one time series
  measurements(series, t, value)  -- t = Unix seconds UTC (measurement time), WITHOUT ROWID

Values are stored in the unit delivered by the source.
Aggregation (hour/day) follows each parameter's method: mean (with min/max band), sum, max, min,
or dir (vector mean of directions). Bins are labelled with their start (UTC).
Values with time_ref = interval_end are assigned to the interval in which they were measured
(the 14:00 value for 13:50–14:00 belongs to the hour 13:00–14:00).
"""
from __future__ import annotations

import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .model import ParameterInfo

SCHEMA_VERSION = 3
STATION_META = ("canton", "lat", "lon", "height_masl")
AGG_SECONDS = {"hour": 3600, "day": 86400}
SQL_AGG = {"mean": "AVG(value)", "sum": "SUM(value)", "max": "MAX(value)", "min": "MIN(value)"}
SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    station_id TEXT NOT NULL,
    name TEXT NOT NULL,
    canton TEXT,
    lat REAL,
    lon REAL,
    height_masl REAL,
    UNIQUE (source, station_id)
);
CREATE TABLE IF NOT EXISTS parameters (
    source TEXT NOT NULL,
    code TEXT NOT NULL,
    description TEXT NOT NULL,
    unit TEXT NOT NULL,
    grp TEXT NOT NULL,
    agg TEXT NOT NULL,                      -- mean | sum | max | min | dir
    quantity TEXT,                          -- cross-source key, e.g. air_temperature
    PRIMARY KEY (source, code)
);
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    station INTEGER NOT NULL REFERENCES stations(id),
    parameter TEXT NOT NULL,
    unit TEXT NOT NULL,
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
UPSERT_PARAM = ("INSERT INTO parameters (source, code, description, unit, grp, agg, quantity) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source, code) DO UPDATE SET "
                "description = excluded.description, unit = excluded.unit, grp = excluded.grp, "
                "agg = excluded.agg, quantity = excluded.quantity")
SERIES_KEY = ["source", "station_id", "station_name", "parameter", "unit", "time_ref", "interval_min"]

Describe = Callable[[str, str], ParameterInfo]


class SchemaError(RuntimeError):
    pass


class MeasurementStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'series'").fetchone()[0]
        if has_tables and version < 2:
            raise SchemaError(f"{path} uses an older schema. Delete the file and run 'update' again.")
        if has_tables and version == 2:  # version 3 only adds station metadata columns
            for col, typ in zip(STATION_META, ("TEXT", "REAL", "REAL", "REAL")):
                self.conn.execute(f"ALTER TABLE stations ADD COLUMN {col} {typ}")
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.lock = threading.Lock()
        self._series_cache: dict = {}
        self._known_params: set = set()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- Writing
    def upsert(self, df: pd.DataFrame, describe: Optional[Describe] = None) -> int:
        """Write a canonical frame; existing (series, t) rows are overwritten.
        describe(code, unit) supplies catalogue entries for parameters not yet registered."""
        if df is None or df.empty:
            return 0
        df = df.copy()
        df["interval_min"] = df["interval_min"].fillna(-1).astype(int)
        df["t"] = (df["time_utc"].astype("datetime64[ns, UTC]").astype("int64") // 10**9).astype(int)
        n = 0
        with self.lock, self.conn:
            for key, g in df.groupby(SERIES_KEY, sort=False):
                source, parameter, unit = key[0], key[3], key[4]
                if (source, parameter) not in self._known_params:
                    info = describe(parameter, unit) if describe else ParameterInfo(source, parameter, parameter, unit)
                    self._register(info, unit)
                sid = self._series_id(*key)
                self.conn.executemany(UPSERT, zip([sid] * len(g), g["t"].tolist(), g["value"].astype(float).tolist()))
                n += len(g)
        return n

    def _register(self, info: ParameterInfo, unit: str) -> None:
        self.conn.execute(UPSERT_PARAM, (info.source, info.code, info.description, info.unit or unit,
                                         info.group, info.agg, info.quantity))
        self._known_params.add((info.source, info.code))

    def register_parameters(self, infos) -> None:
        with self.lock, self.conn:
            for info in infos:
                self._register(info, info.unit)

    def set_station_meta(self, source: str, station_id: str, name: str, canton=None, lat=None, lon=None,
                         height_masl=None) -> None:
        """Store station metadata from the configuration; missing values keep what is already stored."""
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO stations (source, station_id, name, canton, lat, lon, height_masl) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source, station_id) DO UPDATE SET "
                "name = excluded.name, canton = COALESCE(excluded.canton, canton), lat = COALESCE(excluded.lat, lat), "
                "lon = COALESCE(excluded.lon, lon), height_masl = COALESCE(excluded.height_masl, height_masl)",
                (source, str(station_id), name or str(station_id), canton, lat, lon, height_masl))

    def _series_id(self, source, station_id, station_name, parameter, unit, time_ref, interval_min) -> int:
        key = (source, station_id, parameter, time_ref, int(interval_min))
        if key in self._series_cache:
            return self._series_cache[key]
        self.conn.execute("INSERT INTO stations (source, station_id, name) VALUES (?, ?, ?) "
                          "ON CONFLICT (source, station_id) DO UPDATE SET name = excluded.name",
                          (source, station_id, station_name))
        st = self.conn.execute("SELECT id FROM stations WHERE source = ? AND station_id = ?",
                               (source, station_id)).fetchone()[0]
        self.conn.execute("INSERT OR IGNORE INTO series (station, parameter, unit, time_ref, interval_min) "
                          "VALUES (?, ?, ?, ?, ?)", (st, parameter, unit, time_ref, int(interval_min)))
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
        """All time series with station, parameter catalogue entry and time range (first/last as Unix seconds)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT se.id, st.source, st.station_id, st.name, se.parameter, se.unit, se.time_ref, se.interval_min, "
                "COALESCE(p.description, se.parameter), COALESCE(p.grp, ''), COALESCE(p.agg, 'mean'), p.quantity, "
                "st.canton, st.lat, st.lon, st.height_masl "
                "FROM series se JOIN stations st ON st.id = se.station "
                "LEFT JOIN parameters p ON p.source = st.source AND p.code = se.parameter "
                "ORDER BY st.source, st.name, se.parameter").fetchall()
            out = []
            for r in rows:
                first, last = self.conn.execute(
                    "SELECT (SELECT MIN(t) FROM measurements WHERE series = ?), "
                    "(SELECT MAX(t) FROM measurements WHERE series = ?)", (r[0], r[0])).fetchone()
                out.append(dict(id=r[0], source=r[1], station_id=r[2], station_name=r[3], parameter=r[4], unit=r[5],
                                time_ref=r[6], interval_min=None if r[7] < 0 else r[7], description=r[8],
                                group=r[9], agg=r[10], quantity=r[11], canton=r[12], lat=r[13], lon=r[14],
                                height_masl=r[15], first=first, last=last))
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
        return {"t": [r[0] for r in rows], "value": [r[1] for r in rows], "min": None, "max": None, "n": None,
                "agg": "raw"}

    def aggregate(self, series_id: int, start: int, end: int, agg: str, min_coverage: float = 0.75) -> dict:
        """Hourly/daily values (UTC) using the parameter's aggregation method. Bins with too little coverage
        are dropped when the expected number of values is known (interval values)."""
        w = AGG_SECONDS[agg]
        info = self.series_info(series_id)
        if info is None:
            raise KeyError(series_id)
        method = info["agg"]
        off = -1 if info["time_ref"] == "interval_end" else 0
        a, b = (start // w) * w, -(-end // w) * w
        bin_sql = "((t + ?) / ?) * ?"
        where = "FROM measurements WHERE series = ? AND t >= ? AND t < ?"
        args = (off, w, w, series_id, a - off, b - off)
        with self.lock:
            if method == "dir":
                rows = self.conn.execute(f"SELECT {bin_sql} AS bin, value {where} ORDER BY t", args).fetchall()
            else:
                rows = self.conn.execute(f"SELECT {bin_sql} AS bin, {SQL_AGG[method]}, MIN(value), MAX(value), COUNT(*) "
                                         f"{where} GROUP BY bin ORDER BY bin", args).fetchall()
        if method == "dir":
            rows = vector_mean(rows)
        interval = info["interval_min"]
        expected = w // (interval * 60) if interval and info["time_ref"] != "instant" else None
        need = math.ceil(expected * min_coverage) if expected else 0
        rows = [r for r in rows if r[4] >= need]
        band = method == "mean"
        return {"t": [r[0] for r in rows], "value": [round(r[1], 3) for r in rows],
                "min": [r[2] for r in rows] if band else None, "max": [r[3] for r in rows] if band else None,
                "n": [r[4] for r in rows], "expected": expected, "agg": method}


def vector_mean(rows) -> list:
    """(bin, direction°) rows -> (bin, mean direction°, None, None, n) per bin, using unit vectors."""
    if not rows:
        return []
    df = pd.DataFrame(rows, columns=["bin", "deg"])
    rad = np.deg2rad(df["deg"])
    df["s"], df["c"] = np.sin(rad), np.cos(rad)
    g = df.groupby("bin").agg(s=("s", "mean"), c=("c", "mean"), n=("deg", "size"))
    deg = (np.rad2deg(np.arctan2(g["s"], g["c"])) + 360) % 360
    return [(int(b), float(d), None, None, int(n)) for b, d, n in zip(g.index, deg, g["n"])]
