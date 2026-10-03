"""Zurich water police weather stations (Tiefenbrunnen, Mythenquai).

History: open-data CSV of the City of Zurich (since 2007, column timestamp_utc, wind in m/s).
Recent days: Tecdottir API (linked by Open Data Zurich), because the CSV files are only updated
periodically. Scraping tecson-data.ch is explicitly prohibited by its terms.
"""
from __future__ import annotations

import io
import logging
from datetime import timedelta
from typing import Iterator

import pandas as pd

from ..model import LOCAL_TZ, Period, parse_time, wind_factor
from .base import Source

log = logging.getLogger(__name__)


class WaPo(Source):
    name = "wapo"
    key = "id"
    defaults = {
        "api_url": "https://tecdottir.metaodi.ch",
        "ogd_url": ("https://data.stadt-zuerich.ch/dataset/sid_wapo_wetterstationen/download/"
                    "messwerte_{id}_seit2007-heute.csv"),
        "api_days": 14,       # this many days before "now" come from the API, older data from the CSV file
        "page_size": 500,
        "max_pages": 400,
        "history_start": "2007-04-22",
    }

    def fetch_station(self, st: dict, period: Period) -> Iterator[pd.DataFrame]:
        api_from = self.clock() - timedelta(days=self.cfg["api_days"])
        if period.start < api_from:
            yield from self._from_ogd(st)
        if period.end >= api_from:
            yield from self._from_api(st, Period(max(period.start, api_from), period.end))

    def _frames(self, st: dict, t, air, water, wind_kmh) -> Iterator[pd.DataFrame]:
        # air temperature and wind are 10-minute means (interval end), water temperature is an instantaneous value
        yield self.frame(t, "interval_end", 10, st["id"], st["name"], air_temperature=air, wind_speed=wind_kmh)
        yield self.frame(t, "instant", 0, st["id"], st["name"], water_temperature=water)

    def _from_ogd(self, st: dict) -> Iterator[pd.DataFrame]:
        url = self.cfg["ogd_url"].format(id=st["id"])
        log.info("WaPo %s: lade OGD-Datei", st["id"])
        df = pd.read_csv(io.BytesIO(self.http.get(url).content), encoding="utf-8-sig",
                         usecols=lambda c: c in {"timestamp_utc", "air_temperature", "water_temperature",
                                                 "wind_speed_avg_10min"})
        t = pd.to_datetime(df["timestamp_utc"], utc=True, format="ISO8601", errors="coerce")
        wind = pd.to_numeric(df.get("wind_speed_avg_10min"), errors="coerce") * 3.6
        yield from self._frames(st, t, df.get("air_temperature"), df.get("water_temperature"), wind)

    def _from_api(self, st: dict, period: Period) -> Iterator[pd.DataFrame]:
        local = lambda d: pd.Timestamp(d).tz_convert(LOCAL_TZ).date()
        params = {"startDate": local(period.start).isoformat(),
                  "endDate": (local(period.end) + timedelta(days=1)).isoformat(),
                  "sort": "timestamp_cet asc", "limit": self.cfg["page_size"]}
        for page in range(self.cfg["max_pages"]):
            data = self.http.get(f"{self.cfg['api_url']}/measurements/{st['id']}",
                                 offset=page * self.cfg["page_size"], **params).json()
            result = data.get("result") or []
            rows = []
            for item in result:
                vals = item.get("values", {})
                v = lambda k: (vals.get(k) or {}).get("value")
                # prefer an explicit timestamp with time zone, otherwise local time (CET/CEST)
                t = parse_time(item.get("timestamp")) or parse_time(v("timestamp_cet"), LOCAL_TZ)
                if t is None:
                    continue
                unit = (vals.get("wind_speed_avg_10min") or {}).get("unit") or "m/s"
                w = pd.to_numeric(v("wind_speed_avg_10min"), errors="coerce")
                rows.append((t, v("air_temperature"), v("water_temperature"), w * wind_factor(unit)))
            if rows:
                t, air, water, wind = zip(*rows)
                yield from self._frames(st, list(t), list(air), list(water), list(wind))
            if len(result) < self.cfg["page_size"]:
                return
        log.warning("WaPo %s: max_pages erreicht, Zeitraum evtl. unvollständig", st["id"])
