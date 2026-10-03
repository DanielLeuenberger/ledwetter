"""METAR via aviationweather.gov (NOAA, no API key).

obsTime = observation time (Unix seconds), temp in °C, wspd in knots.
The API keeps only a few days of archive; older periods return nothing.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Iterator, Optional

import pandas as pd

from ..model import Period
from .base import Source

log = logging.getLogger(__name__)


class MetarAWC(Source):
    name = "metar"
    key = "icao"
    per_station = False
    defaults = {"url": "https://aviationweather.gov/api/data/metar", "max_hours": 360}

    def history_start(self, st: Optional[dict] = None) -> datetime:
        return self.clock() - timedelta(hours=self.cfg["max_hours"])

    def hours_for(self, period: Period) -> int:
        hours = math.ceil((self.clock() - period.start).total_seconds() / 3600) + 1
        if hours > self.cfg["max_hours"]:
            log.warning("METAR: Archiv reicht nur %d h zurück, ältere Werte fehlen", self.cfg["max_hours"])
        return max(1, min(hours, self.cfg["max_hours"]))

    def _fetch(self, period: Period) -> Iterator[pd.DataFrame]:
        names = {s["icao"].upper(): s["name"] for s in self.stations}
        res = self.http.get(self.cfg["url"], ids=",".join(names), format="json", hours=self.hours_for(period))
        data = res.json() if res.content else []
        df = pd.DataFrame([m for m in data if m.get("icaoId") in names])
        if df.empty:
            return
        df["t"] = pd.to_datetime(df["obsTime"], unit="s", utc=True)
        for icao, g in df.groupby("icaoId"):
            yield self.frame(g["t"], "instant", 0, icao, names[icao], air_temperature=g.get("temp"),
                             wind_speed=pd.to_numeric(g.get("wspd"), errors="coerce") * 1.852)
