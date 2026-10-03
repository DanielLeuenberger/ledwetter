"""METAR via aviationweather.gov (NOAA, no API key).

obsTime = observation time (Unix seconds). Extracted numeric fields: temp, dewp (°C), wdir (°),
wspd, wgst (kt), visib (statute miles, '10+' -> 10), altim (QNH, hPa), slp (QFF, hPa).
The API keeps only a few days of archive; older periods return nothing.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Iterator, Optional

import pandas as pd

from ..geo import SWITZERLAND_BBOX
from ..model import Period, to_float
from .base import Source

log = logging.getLogger(__name__)


class MetarAWC(Source):
    name = "metar"
    key = "icao"
    per_station = False
    defaults = {"url": "https://aviationweather.gov/api/data/metar",
                "station_url": "https://aviationweather.gov/api/data/stationinfo", "max_hours": 360}
    CATALOG = {
        "temp": ("Lufttemperatur", "°C", "Temperatur", "mean", "air_temperature"),
        "dewp": ("Taupunkt", "°C", "Feuchte", "mean", "dew_point"),
        "wdir": ("Windrichtung", "°", "Wind", "dir", "wind_direction"),
        "wspd": ("Windgeschwindigkeit", "kt", "Wind", "mean", "wind_speed"),
        "wgst": ("Windböen", "kt", "Wind", "max", "wind_gust"),
        "visib": ("Sichtweite", "SM", "Sicht", "mean", "visibility"),
        "altim": ("Luftdruck QNH", "hPa", "Druck", "mean", "pressure_qnh"),
        "slp": ("Luftdruck auf Meereshöhe", "hPa", "Druck", "mean", "pressure_qff"),
    }

    @classmethod
    def discover(cls, ctx, canton: str) -> list:
        """METAR stations in Switzerland (station info service) whose coordinates lie in the canton."""
        lat0, lat1, lon0, lon1 = SWITZERLAND_BBOX
        stations = ctx.cached("metar-stations", lambda: ctx.http.get(
            cls.defaults["station_url"], bbox=f"{lat0},{lon0},{lat1},{lon1}", format="json").json())
        out = []
        for s in stations or []:
            site_types = s.get("siteType") or ["METAR"]
            if "METAR" not in site_types or s.get("country") not in (None, "CH"):
                continue
            try:
                if ctx.geo.contains(canton, float(s["lat"]), float(s["lon"])):
                    out.append({"icao": s["icaoId"], "name": f"{s.get('site') or s['icaoId']} (METAR)",
                                "lat": float(s["lat"]), "lon": float(s["lon"])})
            except Exception as e:
                log.warning("METAR %s: canton lookup failed (%s)", s.get("icaoId"), e)
        return sorted(out, key=lambda s: s["icao"])

    def history_start(self, st: Optional[dict] = None) -> datetime:
        return self.clock() - timedelta(hours=self.cfg["max_hours"])

    def hours_for(self, period: Period) -> int:
        hours = math.ceil((self.clock() - period.start).total_seconds() / 3600) + 1
        if hours > self.cfg["max_hours"]:
            log.warning("METAR: archive covers only %d h, older values are missing", self.cfg["max_hours"])
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
            for code, info in self.catalog().items():
                if code in g.columns:
                    yield self.frame(g["t"], g[code].map(to_float), icao, names[icao], code, info.unit, "instant", 0)
