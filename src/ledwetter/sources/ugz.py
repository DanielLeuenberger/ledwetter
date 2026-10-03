"""City of Zurich, Environmental and Health Protection (UGZ): hourly means from yearly files (long format).

Columns: Datum, Standort, Parameter, Intervall, Einheit, Wert, Status.
Stations: Zch_Stampfenbachstrasse, Zch_Schimmelstrasse, Zch_Rosengartenstrasse (T, Hr, p, RainDur, WD, WVs,
WVv; StrGlo at Stampfenbachstrasse only) and Zch_Heubeeribüel (T, Hr, p). All parameters are extracted.
'Datum' marks the START of the hour: compared with the 10-minute global radiation of MeteoSwiss at
Zürich / Fluntern (about 1.5 km away), hourly StrGlo matches with an RMS difference of 9 W/m² when 'Datum'
is taken as the start of the hour and 75 W/m² when taken as its end (diagnostic run, 3 October 2026).
"""
from __future__ import annotations

import io
import logging
from typing import Iterator

import pandas as pd
import requests

from ..model import LOCAL_TZ, Period, parse_time_series
from .base import Source

log = logging.getLogger(__name__)


class UgzMeteo(Source):
    name = "ugz"
    key = "standort"
    per_station = False
    regions = ("ZH",)
    defaults = {"url_template": ("https://data.stadt-zuerich.ch/dataset/ugz_meteodaten_stundenmittelwerte/"
                                 "download/ugz_ogd_meteo_h1_{year}.csv"),
                "time_ref": "interval_start", "history_start": "1992-01-01"}
    CATALOG = {
        "T": ("Lufttemperatur", "°C", "Temperatur", "mean", "air_temperature"),
        "Hr": ("Relative Luftfeuchtigkeit", "%Hr", "Feuchte", "mean", "relative_humidity"),
        "p": ("Luftdruck", "hPa", "Druck", "mean", "pressure_qfe"),
        "RainDur": ("Niederschlagsdauer", "min", "Niederschlag", "sum", "rain_duration"),
        "StrGlo": ("Globalstrahlung", "W/m2", "Strahlung", "mean", "global_radiation"),
        "WD": ("Windrichtung", "°", "Wind", "dir", "wind_direction"),
        "WVs": ("Windgeschwindigkeit skalar", "m/s", "Wind", "mean", "wind_speed"),
        "WVv": ("Windgeschwindigkeit vektoriell", "m/s", "Wind", "mean", "wind_speed_vector"),
    }

    @classmethod
    def discover(cls, ctx, canton: str) -> list:
        """Locations present in the current yearly file."""
        year = ctx.clock().year
        try:
            content = ctx.http.get(cls.defaults["url_template"].format(year=year)).content
            df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig", usecols=["Standort"])
        except Exception as e:
            log.warning("UGZ: yearly file %d not available (%s)", year, e)
            return []
        return [{"standort": s, "name": f"{s.removeprefix('Zch_')} (UGZ)"} for s in sorted(df["Standort"].unique())]

    def _fetch(self, period: Period) -> Iterator[pd.DataFrame]:
        wanted = {s["standort"]: s["name"] for s in self.stations}
        y0 = pd.Timestamp(period.start).tz_convert(LOCAL_TZ).year
        y1 = pd.Timestamp(period.end).tz_convert(LOCAL_TZ).year
        for year in range(y0, y1 + 1):
            try:
                content = self.http.get(self.cfg["url_template"].format(year=year)).content
            except requests.HTTPError as e:
                log.warning("UGZ %d: %s", year, e)
                continue
            log.info("UGZ: yearly file %d loaded", year)
            df = pd.read_csv(io.BytesIO(content), encoding="utf-8-sig",
                             usecols=["Datum", "Standort", "Parameter", "Einheit", "Wert"])
            missing = set(wanted) - set(df["Standort"])
            if missing:
                log.warning("UGZ %d: locations %s missing. Available: %s", year, sorted(missing),
                            sorted(df["Standort"].unique()))
            df = df[df["Standort"].isin(wanted)].copy()
            df["t"] = parse_time_series(df["Datum"], LOCAL_TZ)
            df = df[(df["t"] >= period.start) & (df["t"] <= period.end)]
            for (standort, param), g in df.groupby(["Standort", "Parameter"]):
                unit = str(g["Einheit"].iloc[0]) if g["Einheit"].notna().any() else self.describe(param).unit
                yield self.frame(g["t"], g["Wert"], standort, wanted[standort], param, unit, self.cfg["time_ref"], 60)
