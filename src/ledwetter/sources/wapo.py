"""Zurich water police weather stations (Tiefenbrunnen, Mythenquai).

History: open-data CSV of the City of Zurich (since 2007, column timestamp_utc).
Recent days: Tecdottir API (linked by Open Data Zurich), because the CSV files are only updated
periodically. Scraping tecson-data.ch is explicitly prohibited by its terms.

All measurement columns are extracted. Parameters named *_10min are 10-minute means/maxima
(interval end); for the others the documentation does not state whether they are instantaneous
values or means, so their time reference is 'unknown'.
"""
from __future__ import annotations

import io
import logging
from datetime import timedelta
from typing import Iterator

import pandas as pd

from ..model import LOCAL_TZ, Period, parse_time
from .base import Source

log = logging.getLogger(__name__)
TIME_COLUMNS = {"_id", "timestamp_utc", "timestamp_cet"}


class WaPo(Source):
    name = "wapo"
    key = "id"
    regions = ("ZH",)
    defaults = {
        "api_url": "https://tecdottir.metaodi.ch",
        "ogd_url": ("https://data.stadt-zuerich.ch/dataset/sid_wapo_wetterstationen/download/"
                    "messwerte_{id}_seit2007-heute.csv"),
        "api_days": 14,       # this many days before "now" come from the API, older data from the CSV file
        "page_size": 500,
        "max_pages": 400,
        "history_start": "2007-04-22",
    }
    CATALOG = {
        "air_temperature": ("Lufttemperatur 2 m", "°C", "Temperatur", "mean", "air_temperature"),
        "water_temperature": ("Wassertemperatur 1 m Tiefe", "°C", "Wasser", "mean", "water_temperature"),
        "wind_gust_max_10min": ("Windböen, Maximum 10 min", "m/s", "Wind", "max", "wind_gust"),
        "wind_speed_avg_10min": ("Windgeschwindigkeit, Mittel 10 min", "m/s", "Wind", "mean", "wind_speed"),
        "wind_force_avg_10min": ("Windstärke, Mittel 10 min", "Bft", "Wind", "mean", None),
        "wind_direction": ("Windrichtung", "°", "Wind", "dir", "wind_direction"),
        "windchill": ("Windchill", "°C", "Temperatur", "mean", None),
        "barometric_pressure_qfe": ("Luftdruck QFE", "hPa", "Druck", "mean", "pressure_qfe"),
        "precipitation": ("Niederschlag", "mm", "Niederschlag", "sum", "precipitation"),
        "dew_point": ("Taupunkt", "°C", "Feuchte", "mean", "dew_point"),
        "global_radiation": ("Globalstrahlung", "W/m²", "Strahlung", "mean", "global_radiation"),
        "humidity": ("Relative Luftfeuchte", "%", "Feuchte", "mean", "relative_humidity"),
        "water_level": ("Pegel Zürichsee", "m", "Wasser", "mean", "lake_level"),
    }

    @classmethod
    def discover(cls, ctx, canton: str) -> list:
        return [{"id": "tiefenbrunnen", "name": "Tiefenbrunnen (WaPo)"},
                {"id": "mythenquai", "name": "Mythenquai (WaPo)"}]

    @staticmethod
    def time_reference(code: str) -> tuple:
        return ("interval_end", 10) if code.endswith("_10min") else ("unknown", 10)

    def fetch_station(self, st: dict, period: Period) -> Iterator[pd.DataFrame]:
        api_from = self.clock() - timedelta(days=self.cfg["api_days"])
        if period.start < api_from:
            yield from self._from_ogd(st)
        if period.end >= api_from:
            yield from self._from_api(st, Period(max(period.start, api_from), period.end))

    def _frames(self, st: dict, t, columns: dict, units: dict) -> Iterator[pd.DataFrame]:
        for code, values in columns.items():
            unit = units.get(code) or self.describe(code).unit
            yield self.frame(t, values, st["id"], st["name"], code, unit, *self.time_reference(code))

    def _from_ogd(self, st: dict) -> Iterator[pd.DataFrame]:
        url = self.cfg["ogd_url"].format(id=st["id"])
        log.info("WaPo %s: loading open-data CSV", st["id"])
        df = pd.read_csv(io.BytesIO(self.http.get(url).content), encoding="utf-8-sig", low_memory=False)
        t = pd.to_datetime(df["timestamp_utc"], utc=True, format="ISO8601", errors="coerce")
        cols = {c: df[c] for c in df.columns if c not in TIME_COLUMNS}
        yield from self._frames(st, t, cols, {})

    def _from_api(self, st: dict, period: Period) -> Iterator[pd.DataFrame]:
        local = lambda d: pd.Timestamp(d).tz_convert(LOCAL_TZ).date()
        params = {"startDate": local(period.start).isoformat(),
                  "endDate": (local(period.end) + timedelta(days=1)).isoformat(),
                  "sort": "timestamp_cet asc", "limit": self.cfg["page_size"]}
        for page in range(self.cfg["max_pages"]):
            data = self.http.get(f"{self.cfg['api_url']}/measurements/{st['id']}",
                                 offset=page * self.cfg["page_size"], **params).json()
            result = data.get("result") or []
            times, rows, units = [], [], {}
            for item in result:
                vals = item.get("values", {})
                # prefer an explicit timestamp with time zone, otherwise local time (CET/CEST)
                t = parse_time(item.get("timestamp")) or parse_time((vals.get("timestamp_cet") or {}).get("value"), LOCAL_TZ)
                if t is None:
                    continue
                times.append(t)
                rows.append({k: (v or {}).get("value") for k, v in vals.items() if k not in TIME_COLUMNS})
                units.update({k: (v or {}).get("unit") for k, v in vals.items() if (v or {}).get("unit")})
            if rows:
                df = pd.DataFrame(rows)
                yield from self._frames(st, times, {c: df[c] for c in df.columns}, units)
            if len(result) < self.cfg["page_size"]:
                return
        log.warning("WaPo %s: max_pages reached, period may be incomplete", st["id"])
