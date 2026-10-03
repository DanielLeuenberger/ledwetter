"""MeteoSwiss SwissMetNet (open government data, data.geo.admin.ch).

10-minute values, timestamp = end of interval, UTC. CSV with ';', Windows-1252, 'dd.mm.yyyy HH:MM'.
Files per station: t_historical_<decade>, t_recent (1 January to yesterday), t_now (from yesterday 12 UTC).
The station's STAC item lists which files exist.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

import pandas as pd
import requests

from ..model import Period
from .base import Source

log = logging.getLogger(__name__)
FILE_RE = re.compile(r"_t_(?:historical_(\d{4})-(\d{4})|(recent)|(now))\.csv$")


class MeteoSwissSMN(Source):
    name = "meteoschweiz"
    key = "code"
    defaults = {
        "base_url": "https://data.geo.admin.ch",
        "stac_url": "https://data.geo.admin.ch/api/stac/v1",
        "params": {
            "ogd-smn": {"temp": ["tre200s0"], "wind_kmh": ["fu3010z0"], "wind_ms": ["fkl010z0"]},
            # Towers: the pattern <code>1tow<agg> with digit 1 is documented (dk1towh0, dv1towz0, fk1towd0);
            # the 10-minute names are derived from it. If none matches, the log lists the actual columns.
            "ogd-smn-tower": {"temp": ["ta1tows0", "tre200s0"], "wind_kmh": ["fu1towz0", "fu3010z0"],
                              "wind_ms": ["fk1towz0", "fkl010z0"]},
        },
    }

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
            log.debug("SMN %s: STAC nicht verfügbar (%s), nutze Standardnamen", code, e)
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
    @staticmethod
    def _first(df: pd.DataFrame, candidates: list) -> Optional[str]:
        return next((c for c in candidates if c in df.columns and df[c].notna().any()), None)

    def fetch_station(self, st: dict, period: Period) -> Iterator[pd.DataFrame]:
        coll = st.get("collection", "ogd-smn")
        p = self.cfg["params"].get(coll, self.cfg["params"]["ogd-smn"])
        for url in self.files_for(st["code"], coll, period):
            fname = url.rsplit("/", 1)[-1]
            log.info("SMN %s: lade %s", st["code"], fname)
            try:
                text = self.http.get(url).content.decode("cp1252")
            except requests.HTTPError as e:
                log.warning("SMN %s: %s nicht verfügbar (%s)", st["code"], fname, e)
                continue
            yield self.parse(text, st, p, period)

    def parse(self, text: str, st: dict, p: dict, period: Optional[Period] = None) -> pd.DataFrame:
        df = pd.read_csv(io.StringIO(text), sep=";", low_memory=False)
        t = pd.to_datetime(df["reference_timestamp"], format="%d.%m.%Y %H:%M", utc=True)
        if period is not None:
            mask = (t >= period.start) & (t <= period.end)
            df, t = df[mask], t[mask]
        t_col, w_col = self._first(df, p["temp"]), self._first(df, p["wind_kmh"])
        wms_col = None if w_col else self._first(df, p["wind_ms"])
        if df.empty:
            return df.iloc[0:0]
        if not (t_col or w_col or wms_col):
            log.warning("SMN %s: keine bekannten Parameter. Spalten: %s", st["code"], list(df.columns))
            return df.iloc[0:0]
        wind = (pd.to_numeric(df[w_col], errors="coerce") if w_col else
                pd.to_numeric(df[wms_col], errors="coerce") * 3.6 if wms_col else None)
        return self.frame(t, "interval_end", 10, st["code"], st["name"],
                          air_temperature=df[t_col] if t_col else None, wind_speed=wind)
