"""MeteoSwiss SwissMetNet (open government data, data.geo.admin.ch).

Collections: ogd-smn (weather stations), ogd-smn-tower (tower stations), ogd-smn-precip (precipitation stations).
10-minute files; the timestamp is the end of the 10-minute interval, UTC. CSV with ';', Windows-1252,
'dd.mm.yyyy HH:MM'. Files per station: t_historical_<decade>, t_recent (1 January to yesterday),
t_now (from yesterday 12 UTC). The station's STAC item lists which files exist.

All parameter columns are extracted. Descriptions, units and groups come from the official
<collection>_meta_parameters.csv. Parameter codes encode their type at position 7:
's' = current value at the timestamp (instant), 'z' = 10-minute mean/total/maximum (interval end).
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

import pandas as pd
import requests

from ..model import ParameterInfo, Period, guess_aggregation
from .base import Source

log = logging.getLogger(__name__)
FILE_RE = re.compile(r"_t_(?:historical_(\d{4})-(\d{4})|(recent)|(now))\.csv$")
ID_COLUMNS = {"station_abbr", "reference_timestamp"}

# Cross-source quantities of well-known SwissMetNet parameters
QUANTITY = {
    "tre200s0": "air_temperature", "ta1tows0": "air_temperature", "tre005s0": "air_temperature_5cm",
    "tresurs0": "surface_temperature", "tde200s0": "dew_point", "tdetows0": "dew_point",
    "ure200s0": "relative_humidity", "uretows0": "relative_humidity", "pva200s0": "vapour_pressure",
    "prestas0": "pressure_qfe", "pp0qffs0": "pressure_qff", "pp0qnhs0": "pressure_qnh",
    "rre150z0": "precipitation", "sre000z0": "sunshine_duration", "gre000z0": "global_radiation",
    "ods000z0": "diffuse_radiation", "oli000z0": "longwave_incoming", "olo000z0": "longwave_outgoing",
    "osr000z0": "shortwave_reflected", "dkl010z0": "wind_direction", "dv1towz0": "wind_direction",
    "fu3010z0": "wind_speed", "fu1towz0": "wind_speed", "fu3towz0": "wind_speed",
    "fu3010z1": "wind_gust", "fu1towz1": "wind_gust", "fu3towz1": "wind_gust", "htoauts0": "snow_depth",
    "tso005s0": "soil_temperature_5cm", "tso010s0": "soil_temperature_10cm", "tso020s0": "soil_temperature_20cm",
}
# m/s columns that duplicate a km/h column of the same measurement (kept only if the km/h one is missing)
MS_DUPLICATES = [(r"^fkl010(z\d)$", ("fu3010{}",)), (r"^fk\dtow(z\d)$", ("fu3tow{}", "fu1tow{}"))]


def time_reference(code: str) -> tuple:
    """(time_ref, interval_min) from the code: position 7 's' = instant, otherwise a 10-minute interval."""
    return ("instant", 0) if len(code) >= 7 and code[6] == "s" else ("interval_end", 10)


class MeteoSwissSMN(Source):
    name = "meteoschweiz"
    key = "code"
    defaults = {"base_url": "https://data.geo.admin.ch", "stac_url": "https://data.geo.admin.ch/api/stac/v1"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._catalogs: dict = {}

    # ---------------------------------------------------------------- Station discovery
    @classmethod
    def discover(cls, ctx, canton: str) -> list:
        """Stations of the weather, tower and precipitation networks from the official station lists."""
        out = []
        for coll in ("ogd-smn", "ogd-smn-tower", "ogd-smn-precip"):
            df = ctx.cached(("smn-stations", coll), lambda c=coll: cls._station_list(ctx.http, c))
            if df is None:
                continue
            for r in df[df["station_canton"] == canton].itertuples(index=False):
                entry = {"code": r.station_abbr, "name": r.station_name, "collection": coll,
                         "lat": float(r.station_coordinates_wgs84_lat), "lon": float(r.station_coordinates_wgs84_lon),
                         "height_masl": float(r.station_height_masl)}
                if coll == "ogd-smn":
                    entry.pop("collection")
                out.append(entry)
        return out

    @classmethod
    def _station_list(cls, http, coll: str):
        url = f"{cls.defaults['base_url']}/ch.meteoschweiz.{coll}/{coll}_meta_stations.csv"
        try:
            return pd.read_csv(io.StringIO(http.get(url).content.decode("cp1252")), sep=";")
        except Exception as e:
            log.warning("MeteoSwiss %s: station list not available (%s)", coll, e)
            return None

    # ---------------------------------------------------------------- Parameter catalogue
    def collection_catalog(self, coll: str) -> dict:
        """Official parameter metadata of a collection (cached per run)."""
        if coll not in self._catalogs:
            url = f"{self.cfg['base_url']}/ch.meteoschweiz.{coll}/{coll}_meta_parameters.csv"
            cat = {}
            try:
                df = pd.read_csv(io.StringIO(self.http.get(url).content.decode("cp1252")), sep=";")
                for r in df.itertuples(index=False):
                    if getattr(r, "parameter_granularity", "T") != "T":
                        continue
                    code = r.parameter_shortname
                    en = str(getattr(r, "parameter_description_en", ""))
                    cat[code] = ParameterInfo(self.name, code, str(r.parameter_description_de), str(r.parameter_unit),
                                              str(r.parameter_group_de), guess_aggregation(code, en), QUANTITY.get(code))
            except Exception as e:
                log.warning("SMN: parameter metadata for %s not available (%s)", coll, e)
            self._catalogs[coll] = cat
        return self._catalogs[coll]

    def catalog(self) -> dict:
        merged = {}
        for coll in ("ogd-smn", "ogd-smn-tower", "ogd-smn-precip"):
            merged.update(self._catalogs.get(coll, {}))
        return merged

    def describe(self, code: str, unit: str = "") -> ParameterInfo:
        info = self.catalog().get(code)
        if info is not None:
            return info
        return ParameterInfo(self.name, code, code, unit or "", "", guess_aggregation(code), QUANTITY.get(code))

    # ---------------------------------------------------------------- Files
    def available_files(self, code: str, coll: str) -> list:
        """10-minute files listed in the STAC item; falls back to computed standard names on error."""
        url = f"{self.cfg['stac_url']}/collections/ch.meteoschweiz.{coll}/items/{code.lower()}"
        try:
            assets = self.http.get(url).json().get("assets", {})
            files = [a["href"] for a in assets.values() if FILE_RE.search(a.get("href", ""))]
            if files:
                return files
        except Exception as e:
            log.debug("SMN %s: STAC not available (%s), using standard names", code, e)
        c = code.lower()
        base = f"{self.cfg['base_url']}/ch.meteoschweiz.{coll}/{c}/{coll}_{c}_t_"
        decades = range(2000, self.clock().year // 10 * 10 + 1, 10)
        return [f"{base}historical_{d}-{d + 9}.csv" for d in decades] + [f"{base}recent.csv", f"{base}now.csv"]

    def coverage(self, url: str) -> tuple:
        """Time range a file covers according to the naming convention."""
        m = FILE_RE.search(url)
        now = self.clock()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        year_start = today.replace(month=1, day=1)
        if m.group(1):  # historical files end on 31 December of the previous year at the latest
            return (datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc),
                    min(datetime(int(m.group(2)) + 1, 1, 1, tzinfo=timezone.utc), year_start))
        if m.group(3):
            return year_start, today
        return today - timedelta(hours=12), now + timedelta(hours=1)

    def files_for(self, code: str, coll: str, period: Period) -> list:
        files = [u for u in self.available_files(code, coll) if period.overlaps(*self.coverage(u))]
        return sorted(files, key=lambda u: self.coverage(u)[0])

    def history_start(self, st: Optional[dict] = None) -> datetime:
        if "history_start" in self.cfg or st is None:
            return super().history_start(st)
        files = self.available_files(st["code"], st.get("collection", "ogd-smn"))
        return min(self.coverage(u)[0] for u in files)

    # ---------------------------------------------------------------- Fetching
    def fetch_station(self, st: dict, period: Period) -> Iterator[pd.DataFrame]:
        coll = st.get("collection", "ogd-smn")
        self.collection_catalog(coll)
        for url in self.files_for(st["code"], coll, period):
            fname = url.rsplit("/", 1)[-1]
            log.info("SMN %s: loading %s", st["code"], fname)
            try:
                text = self.http.get(url).content.decode("cp1252")
            except requests.HTTPError as e:
                log.warning("SMN %s: %s not available (%s)", st["code"], fname, e)
                continue
            yield from self.parse(text, st, coll, period)

    @staticmethod
    def drop_duplicate_units(columns: list) -> list:
        cols = set(columns)
        keep = []
        for c in columns:
            dup = False
            for pattern, templates in MS_DUPLICATES:
                m = re.match(pattern, c)
                if m and any(t.format(*m.groups()) in cols for t in templates):
                    dup = True
            if not dup:
                keep.append(c)
        return keep

    def parse(self, text: str, st: dict, coll: str = "ogd-smn", period: Optional[Period] = None) -> Iterator[pd.DataFrame]:
        df = pd.read_csv(io.StringIO(text), sep=";", low_memory=False)
        t = pd.to_datetime(df["reference_timestamp"], format="%d.%m.%Y %H:%M", utc=True)
        if period is not None:
            mask = (t >= period.start) & (t <= period.end)
            df, t = df[mask], t[mask]
        if df.empty:
            return
        cols = [c for c in df.columns if c not in ID_COLUMNS and df[c].notna().any()]
        cols = self.drop_duplicate_units(cols)
        if not cols:
            log.warning("SMN %s: no parameter columns with data. Columns: %s", st["code"], list(df.columns))
            return
        cat = self.collection_catalog(coll)
        for code in cols:
            time_ref, interval = time_reference(code)
            unit = cat[code].unit if code in cat else ""
            yield self.frame(t, df[code], st["code"], st["name"], code, unit, time_ref, interval)
