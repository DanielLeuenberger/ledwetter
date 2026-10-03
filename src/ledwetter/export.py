"""Homogeneous CSV export (long format)."""
from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from .model import COLUMNS, Period, empty_frame

log = logging.getLogger(__name__)
KEY = ["time_utc", "source", "station_id", "parameter", "time_ref"]


def collect(sources: list, period: Period) -> pd.DataFrame:
    """Query all sources; a failing source does not abort the export."""
    frames = []
    for src in sources:
        try:
            got = list(src.fetch(period))
            n = sum(len(f) for f in got)
            log.info("%-12s %7d Messwerte", src.name, n)
            frames.extend(got)
        except Exception:
            log.exception("Quelle %s fehlgeschlagen", src.name)
    return pd.concat(frames, ignore_index=True) if frames else empty_frame()


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicates (e.g. recent/now overlap), sort, format times as ISO strings."""
    if df.empty:
        return df[COLUMNS]
    df = df.drop_duplicates(subset=KEY, keep="last")
    df = df.sort_values(["source", "station_id", "parameter", "time_ref", "time_utc"]).reset_index(drop=True)
    df = df.copy()
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df["interval_min"] = df["interval_min"].astype("Int64")
    df["value"] = df["value"].round(3)
    return df[COLUMNS]


def write_csv(frames: Iterable[pd.DataFrame] | pd.DataFrame, path: str) -> int:
    df = frames if isinstance(frames, pd.DataFrame) else pd.concat(list(frames), ignore_index=True)
    df = normalize(df)
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")
    return len(df)
