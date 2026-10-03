"""Canonical data format, parameter catalogue, time period and parsing helpers.

Every source yields pandas DataFrames with the columns COLUMNS (long format, one measurement per row).
Values are stored in the unit delivered by the source; the parameter catalogue describes each
parameter (description, unit, group, aggregation method and an optional cross-source quantity).
"""
from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

import pandas as pd

LOCAL_TZ = "Europe/Zurich"
TIME_REFS = ("interval_end", "interval_start", "instant", "unknown")
AGGREGATIONS = ("mean", "sum", "max", "min", "dir")
COLUMNS = ["time_utc", "time_ref", "interval_min", "source", "station_id", "station_name",
           "parameter", "value", "unit"]

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ Parameter catalogue
@dataclass(frozen=True)
class ParameterInfo:
    """Describes one parameter of one source.

    agg:      how values are combined into hourly/daily values (mean, sum, max, min, dir = vector mean)
    quantity: optional cross-source key (e.g. air_temperature) so that equivalent parameters of
              different sources can be shown together
    """
    source: str
    code: str
    description: str
    unit: str
    group: str = ""
    agg: str = "mean"
    quantity: Optional[str] = None


def guess_aggregation(code: str, description: str = "") -> str:
    """Aggregation method from an (English or German) description, falling back to the code."""
    if not description:
        return _aggregation_from_code(code)
    d = description.lower()
    if "direction" in d or "richtung" in d:
        return "dir"
    if "total" in d or "summe" in d or "duration" in d or "dauer" in d:
        return "sum"
    if "maximum" in d or "gust" in d or "böe" in d or "index" in d:
        return "max"
    if "minimum" in d:
        return "min"
    return "mean"


def _aggregation_from_code(code: str) -> str:
    """Rules for MeteoSwiss-style codes (e.g. rre150z0) when no description is available."""
    c = code.lower()
    if c.startswith(("rre", "rka", "sre", "erefao")) or c in ("raindur", "precipitation"):
        return "sum"
    if c.startswith(("dkl", "dv")) or "direction" in c or c in ("wd", "wdir"):
        return "dir"
    if re.fullmatch(r"[a-z0-9]{6}z[13]", c) or "gust" in c or c == "wgst":
        return "max"
    return "mean"


class ParameterFilter:
    """include/exclude lists with shell-style patterns, e.g. {include: ["tre*"], exclude: ["fkl*"]}."""

    def __init__(self, include: Optional[Iterable[str]] = None, exclude: Optional[Iterable[str]] = None):
        self.include = list(include) if include else None
        self.exclude = list(exclude or [])

    def __call__(self, code: str) -> bool:
        if self.include is not None and not any(fnmatch.fnmatchcase(code, p) for p in self.include):
            return False
        return not any(fnmatch.fnmatchcase(code, p) for p in self.exclude)


# ------------------------------------------------------------------ Frames
def empty_frame() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})
    df["time_utc"] = pd.Series(dtype="datetime64[ns, UTC]")
    df["value"] = pd.Series(dtype="float64")
    df["interval_min"] = pd.Series(dtype="Int64")
    return df


def make_frame(times, values, *, source: str, station_id: str, station_name: str, parameter: str,
               unit: str, time_ref: str, interval_min: Optional[int]) -> pd.DataFrame:
    """Build a canonical frame for one parameter from aligned times and values. Missing values are dropped."""
    if time_ref not in TIME_REFS:
        raise ValueError(f"Unknown time_ref: {time_ref}")
    t = pd.Series(pd.to_datetime(pd.Series(times).reset_index(drop=True), utc=True))
    v = pd.to_numeric(pd.Series(values).reset_index(drop=True), errors="coerce").astype("float64")
    df = pd.DataFrame({"time_utc": t, "value": v}).dropna()
    if df.empty:
        return empty_frame()
    df["time_ref"] = time_ref
    df["interval_min"] = pd.array([interval_min] * len(df), dtype="Int64")
    df["source"], df["station_id"], df["station_name"] = source, str(station_id), station_name
    df["parameter"], df["unit"] = parameter, unit or ""
    return df[COLUMNS].reset_index(drop=True)


def concat(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    frames = [f for f in frames if f is not None and not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else empty_frame()


# ------------------------------------------------------------------ Units and times
WIND_FACTORS_KMH = {"m/s": 3.6, "km/h": 1.0, "kt": 1.852, "kn": 1.852}


def to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(str(v).strip().replace(",", ".").rstrip("+"))
    except ValueError:
        return None
    return None if math.isnan(f) else f


def parse_time(value, assume_tz: str = "UTC") -> Optional[datetime]:
    """Parse ISO 8601, 'dd.mm.yyyy HH:MM[:SS]' or Unix seconds into UTC.
    Naive times are interpreted in assume_tz; ambiguous or non-existent local times (DST) yield None."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            ts = pd.Timestamp(value, unit="s", tz="UTC")
        else:
            s = str(value).strip()
            if not s:
                return None
            ts = pd.Timestamp(pd.to_datetime(s, dayfirst="." in s[:5]))
        if ts.tzinfo is None:
            ts = ts.tz_localize(assume_tz, ambiguous="NaT", nonexistent="NaT")
        return None if pd.isna(ts) else ts.tz_convert("UTC").to_pydatetime()
    except (ValueError, TypeError, OverflowError):
        return None


_OFFSET_RE = r"(?:Z|[+-]\d{2}:?\d{2})$"


def parse_time_series(s: pd.Series, assume_tz: str) -> pd.Series:
    """Vectorised variant: values with an offset go straight to UTC, naive values are localised to assume_tz."""
    s = s.astype(str).str.strip()
    has_offset = s.str.contains(_OFFSET_RE, regex=True)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns, UTC]")
    if has_offset.any():
        out[has_offset] = pd.to_datetime(s[has_offset], utc=True, format="ISO8601", errors="coerce")
    if (~has_offset).any():
        naive = pd.to_datetime(s[~has_offset], format="ISO8601", errors="coerce")
        out[~has_offset] = naive.dt.tz_localize(assume_tz, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")
    return out


# ------------------------------------------------------------------ Time period
class Period:
    """Closed time period [start, end] in UTC."""

    def __init__(self, start: datetime, end: datetime):
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Period requires timezone-aware datetimes.")
        if start >= end:
            raise ValueError("Start must be before end.")
        self.start, self.end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    @classmethod
    def from_args(cls, start: Optional[str], end: Optional[str], now: Optional[datetime] = None) -> "Period":
        now = now or utcnow()
        e = min(cls.parse_bound(end, is_end=True), now) if end else now
        s = cls.parse_bound(start, is_end=False) if start else e - timedelta(hours=24)
        return cls(s, e)

    @staticmethod
    def parse_bound(text: str, is_end: bool) -> datetime:
        """Naive input is local time (Europe/Zurich); a date-only end includes the whole day."""
        ts = pd.Timestamp(text)
        if is_end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text.strip()):
            ts += pd.Timedelta(days=1)
        if ts.tzinfo is None:
            ts = ts.tz_localize(LOCAL_TZ)
        return ts.tz_convert("UTC").to_pydatetime()

    def contains(self, t: datetime) -> bool:
        return self.start <= t <= self.end

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return start <= self.end and end >= self.start

    def __eq__(self, other) -> bool:
        return isinstance(other, Period) and (self.start, self.end) == (other.start, other.end)

    def __repr__(self) -> str:
        return f"Period({self.start:%Y-%m-%d %H:%M} – {self.end:%Y-%m-%d %H:%M} UTC)"
