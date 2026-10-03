"""Base class of all data sources."""
from __future__ import annotations

import copy
import logging
from abc import ABC
from datetime import datetime
from typing import Iterator, Optional

import pandas as pd

from ..http import HttpClient
from ..model import Clock, Period, make_frame, utcnow

log = logging.getLogger(__name__)


class Source(ABC):
    """Yields canonical DataFrames (see model.COLUMNS) for a time period.

    Subclasses set name, key (the station key in the configuration) and defaults, and implement
    either fetch_station (one station at a time) or _fetch (one batch request, per_station=False).
    """

    name: str = ""
    key: str = "id"
    per_station: bool = True
    defaults: dict = {}

    def __init__(self, http: HttpClient, cfg: Optional[dict] = None, clock: Clock = utcnow):
        self.http = http
        self.cfg = {**copy.deepcopy(self.defaults), **(cfg or {})}
        self.clock = clock

    # ---------------------------------------------------------------- Stations
    @property
    def stations(self) -> list:
        return list(self.cfg.get("stations") or [])

    def station_key(self, st: dict) -> str:
        return str(st[self.key])

    def with_stations(self, stations: list) -> "Source":
        clone = copy.copy(self)
        clone.cfg = {**self.cfg, "stations": stations}
        return clone

    def history_start(self, st: Optional[dict] = None) -> datetime:
        """Earliest time for which the source offers data (used for backfills)."""
        return pd.Timestamp(self.cfg.get("history_start", "2000-01-01"), tz="UTC").to_pydatetime()

    # ---------------------------------------------------------------- Fetching
    def fetch(self, period: Period) -> Iterator[pd.DataFrame]:
        """Yield canonical frames clipped to the period. Empty frames are skipped."""
        if not self.stations:
            return
        for df in self._fetch(period):
            if df is None or df.empty:
                continue
            df = df[(df["time_utc"] >= period.start) & (df["time_utc"] <= period.end)]
            if not df.empty:
                yield df.reset_index(drop=True)

    def _fetch(self, period: Period) -> Iterator[pd.DataFrame]:
        for st in self.stations:
            try:
                yield from self.fetch_station(st, period)
            except Exception as e:  # one failing station must not stop the others
                log.warning("%s %s: %s", self.name, self.station_key(st), e)

    def fetch_station(self, st: dict, period: Period) -> Iterator[pd.DataFrame]:
        raise NotImplementedError

    def frame(self, times, time_ref: str, interval_min: Optional[int], station_id: str,
              station_name: str, **params) -> pd.DataFrame:
        return make_frame(times, source=self.name, station_id=station_id, station_name=station_name,
                          time_ref=time_ref, interval_min=interval_min, **params)
