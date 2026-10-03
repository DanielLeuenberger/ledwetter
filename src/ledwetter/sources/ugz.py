"""City of Zurich, Environmental and Health Protection (UGZ): hourly means from yearly files (long format).

Columns: Datum, Standort, Parameter, Intervall, Einheit, Wert, Status.
Whether 'Datum' marks the start or the end of the hour is not verified -> time_ref = unknown.
"""
from __future__ import annotations

import io
import logging
from typing import Iterator

import pandas as pd
import requests

from ..model import LOCAL_TZ, Period, WIND_FACTORS, parse_time_series
from .base import Source

log = logging.getLogger(__name__)


class UgzMeteo(Source):
    name = "ugz"
    key = "standort"
    per_station = False
    defaults = {"url_template": ("https://data.stadt-zuerich.ch/dataset/ugz_meteodaten_stundenmittelwerte/"
                                 "download/ugz_ogd_meteo_h1_{year}.csv"),
                "temp_params": ["T"], "wind_params": ["WVs", "WVv"], "time_ref": "unknown",
                "history_start": "1992-01-01"}

    def _fetch(self, period: Period) -> Iterator[pd.DataFrame]:
        wanted = {s["standort"]: s["name"] for s in self.stations}
        params = self.cfg["temp_params"] + self.cfg["wind_params"]
        y0 = pd.Timestamp(period.start).tz_convert(LOCAL_TZ).year
        y1 = pd.Timestamp(period.end).tz_convert(LOCAL_TZ).year
        for year in range(y0, y1 + 1):
            try:
                content = self.http.get(self.cfg["url_template"].format(year=year)).content
            except requests.HTTPError as e:
                log.warning("UGZ %d: %s", year, e)
                continue
            log.info("UGZ: Jahresdatei %d geladen", year)
            df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig",
                             usecols=["Datum", "Standort", "Parameter", "Einheit", "Wert"])
            missing = set(wanted) - set(df["Standort"])
            if missing:
                log.warning("UGZ %d: Standorte %s fehlen. Vorhanden: %s", year, sorted(missing),
                            sorted(df["Standort"].unique()))
            df = df[df["Standort"].isin(wanted) & df["Parameter"].isin(params)].copy()
            df["t"] = parse_time_series(df["Datum"], LOCAL_TZ)
            df = df[(df["t"] >= period.start) & (df["t"] <= period.end)]
            yield from self._frames(df, wanted)

    def _frames(self, df: pd.DataFrame, wanted: dict) -> Iterator[pd.DataFrame]:
        for standort, g in df.groupby("Standort"):
            temp = g[g["Parameter"].isin(self.cfg["temp_params"])]
            yield self.frame(temp["t"], self.cfg["time_ref"], 60, standort, wanted[standort],
                             air_temperature=temp["Wert"])
            # only the first available wind parameter, so that wind does not appear twice
            wp = next((p for p in self.cfg["wind_params"] if (g["Parameter"] == p).any()), None)
            if wp:
                w = g[g["Parameter"] == wp]
                factor = w["Einheit"].map(lambda u: WIND_FACTORS.get(str(u).strip().lower()))
                if factor.isna().any():
                    log.warning("UGZ %s: unbekannte Windeinheit %s", standort, sorted(w["Einheit"].unique()))
                kmh = pd.to_numeric(w["Wert"], errors="coerce") * factor
                yield self.frame(w["t"], self.cfg["time_ref"], 60, standort, wanted[standort], wind_speed=kmh)
