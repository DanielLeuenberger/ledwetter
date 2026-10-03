"""Base class of all data sources."""
from __future__ import annotations

import copy
import logging
from abc import ABC
from datetime import datetime
from typing import Iterator, Optional

import pandas as pd

from ..http import HttpClient
from ..model import Clock, ParameterFilter, ParameterInfo, Period, empty_frame, guess_aggregation, make_frame, utcnow

log = logging.getLogger(__name__)


class Source(ABC):
    """Yields canonical DataFrames (see model.COLUMNS) for a time period.

    Subclasses set name, key (the station key in the configuration), defaults and CATALOG, and implement
    either fetch_station (one station at a time) or _fetch (one batch request, per_station=False).
    All parameters a source delivers are extracted; the optional configuration keys
    parameters.include / parameters.exclude (shell-style patterns) restrict them.
    """

    name: str = ""
    key: str = "id"
    per_station: bool = True
    regions: Optional[tuple] = None   # canton codes the source covers; None = whole Switzerland
    defaults: dict = {}
    CATALOG: dict = {}   # code -> (description, unit, group, agg, quantity)

    def __init__(self, http: HttpClient, cfg: Optional[dict] = None, clock: Clock = utcnow):
        self.http = http
        self.cfg = {**copy.deepcopy(self.defaults), **(cfg or {})}
        self.clock = clock
        p = self.cfg.get("parameters") or {}
        self.wanted = ParameterFilter(p.get("include"), p.get("exclude"))

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

    @classmethod
    def discover(cls, ctx, canton: str) -> list:
        """Station entries of this source within a canton (see discover.DiscoveryContext). Default: none."""
        return []

    def history_start(self, st: Optional[dict] = None) -> datetime:
        """Earliest time for which the source offers data (used for backfills)."""
        return pd.Timestamp(self.cfg.get("history_start", "2000-01-01"), tz="UTC").to_pydatetime()

    # ---------------------------------------------------------------- Parameters
    def catalog(self) -> dict:
        """code -> ParameterInfo for all parameters the source is known to deliver."""
        return {code: ParameterInfo(self.name, code, *spec) for code, spec in self.CATALOG.items()}

    def describe(self, code: str, unit: str = "") -> ParameterInfo:
        """Catalogue entry for a parameter; unknown parameters get a generic description."""
        info = self.catalog().get(code)
        if info is not None:
            return info
        return ParameterInfo(self.name, code, code, unit or "", "", guess_aggregation(code))

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

    def frame(self, times, values, station_id: str, station_name: str, parameter: str, unit: str,
              time_ref: str, interval_min: Optional[int]) -> pd.DataFrame:
        """Canonical frame for one parameter; parameters excluded by the configuration yield nothing."""
        if not self.wanted(parameter):
            return empty_frame()
        return make_frame(times, values, source=self.name, station_id=station_id, station_name=station_name,
                          parameter=parameter, unit=unit, time_ref=time_ref, interval_min=interval_min)
