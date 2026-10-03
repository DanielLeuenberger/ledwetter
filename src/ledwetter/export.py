"""Homogeneous CSV export (long format)."""
from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from .model import COLUMNS, Period, concat

log = logging.getLogger(__name__)
KEY = ["time_utc", "source", "station_id", "parameter", "time_ref"]
EXPORT_COLUMNS = COLUMNS + ["quantity", "description"]


def collect(sources: list, period: Period) -> pd.DataFrame:
    """Query all sources and attach catalogue information; a failing source does not abort the export."""
    frames = []
    for src in sources:
        try:
            got = concat(src.fetch(period))
            if not got.empty:
                infos = {p: src.describe(p, u) for p, u in got[["parameter", "unit"]].drop_duplicates().itertuples(index=False)}
                got["quantity"] = got["parameter"].map(lambda p: infos[p].quantity)
                got["description"] = got["parameter"].map(lambda p: infos[p].description)
            log.info("%-12s %8d values", src.name, len(got))
            frames.append(got)
        except Exception:
            log.exception("Source %s failed", src.name)
    df = concat(frames)
    for c in ("quantity", "description"):
        if c not in df.columns:
            df[c] = pd.Series(dtype="object")
    return df


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicates (e.g. recent/now overlap), sort, format times as ISO strings."""
    cols = [c for c in EXPORT_COLUMNS if c in df.columns]
    if df.empty:
        return df[cols]
    df = df.drop_duplicates(subset=KEY, keep="last")
    df = df.sort_values(["source", "station_id", "parameter", "time_ref", "time_utc"]).reset_index(drop=True).copy()
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    df["interval_min"] = df["interval_min"].astype("Int64")
    df["value"] = df["value"].round(3)
    return df[cols]


def write_csv(frames: Iterable[pd.DataFrame] | pd.DataFrame, path: str) -> int:
    df = frames if isinstance(frames, pd.DataFrame) else concat(frames)
    df = normalize(df)
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n")
    return len(df)
