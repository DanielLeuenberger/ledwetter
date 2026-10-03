"""Canonical data format, time period and parsing helpers.

Every source yields pandas DataFrames with the columns COLUMNS (long format, one measurement per row).
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

LOCAL_TZ = "Europe/Zurich"
UNITS = {"air_temperature": "°C", "water_temperature": "°C", "wind_speed": "km/h"}
MEDIUM = {"air_temperature": "air", "water_temperature": "water", "wind_speed": "air"}
TIME_REFS = ("interval_end", "interval_start", "instant", "unknown")
COLUMNS = ["time_utc", "time_ref", "interval_min", "source", "station_id", "station_name",
           "medium", "parameter", "value", "unit"]
WIND_FACTORS = {"m/s": 3.6, "ms-1": 3.6, "km/h": 1.0, "kmh": 1.0, "kt": 1.852, "kts": 1.852, "kn": 1.852}

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ Frames
def empty_frame() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})
    df["time_utc"] = pd.Series(dtype="datetime64[ns, UTC]")
    df["value"] = pd.Series(dtype="float64")
    df["interval_min"] = pd.Series(dtype="Int64")
    return df


def make_frame(times, *, source: str, station_id: str, station_name: str, time_ref: str,
               interval_min: Optional[int], **params) -> pd.DataFrame:
    """Build a canonical frame from a time axis and parameter series of equal length.
    Missing values are dropped; parameters passed as None are ignored."""
    if time_ref not in TIME_REFS:
        raise ValueError(f"Unbekannter time_ref: {time_ref}")
    t = pd.Series(pd.to_datetime(pd.Series(times).reset_index(drop=True), utc=True))
    parts = []
    for param, values in params.items():
        if values is None:
            continue
        if param not in UNITS:
            raise ValueError(f"Unbekannter Parameter: {param}")
        v = pd.to_numeric(pd.Series(values).reset_index(drop=True), errors="coerce").astype("float64")
        df = pd.DataFrame({"time_utc": t, "value": v}).dropna()
        if df.empty:
            continue
        df["time_ref"] = time_ref
        df["interval_min"] = pd.array([interval_min] * len(df), dtype="Int64")
        df["source"], df["station_id"], df["station_name"] = source, str(station_id), station_name
        df["medium"], df["parameter"], df["unit"] = MEDIUM[param], param, UNITS[param]
        parts.append(df[COLUMNS])
    return pd.concat(parts, ignore_index=True) if parts else empty_frame()


# ------------------------------------------------------------------ Units and times
def wind_factor(unit: Optional[str]) -> float:
    u = (unit or "").strip().lower().replace(" ", "")
    if u not in WIND_FACTORS:
        raise ValueError(f"Unbekannte Windeinheit: {unit!r}")
    return WIND_FACTORS[u]


def to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(str(v).strip().replace(",", "."))
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
            raise ValueError("Period braucht zeitzonenbehaftete Zeiten.")
        if start >= end:
            raise ValueError("Start muss vor Ende liegen.")
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
