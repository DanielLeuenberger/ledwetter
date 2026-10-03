"""FOEN (BAFU) hydrology via the data platform data.bafu.admin.ch (GraphQL, no API key).

All parameters of a station are extracted as 10-minute means; timestamp = interval start (UTC).
Common parameters: W (water level), Q (discharge), WT (water temperature).
A query returns at most 10,000 rows, so requests are split into time windows.
Optional: live feed (last 12 h only, instantaneous values, observation time).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Iterator, Optional

import pandas as pd

from ..model import Period, parse_time
from .base import Source

log = logging.getLogger(__name__)
ISO = "%Y-%m-%dT%H:%M:%SZ"


class BafuHydro(Source):
    name = "bafu"
    key = "station_id"
    per_station = False
    defaults = {"endpoint": "https://data.bafu.admin.ch/api", "include_live": False, "row_cap": 9000,
                "params_per_station": 3, "history_start": "1990-01-01"}
    CATALOG = {
        "W": ("Wasserstand", "m ü.M.", "Wasser", "mean", "water_level"),
        "Q": ("Abfluss", "m³/s", "Wasser", "mean", "discharge"),
        "WT": ("Wassertemperatur", "°C", "Wasser", "mean", "water_temperature"),
    }

    @staticmethod
    def _post(http, endpoint: str, q: str) -> dict:
        payload = http.post_json(endpoint, {"query": q})
        if "errors" in payload:
            raise RuntimeError(payload["errors"])
        return payload["data"]["water"]["observations"]

    def _query(self, q: str) -> dict:
        return self._post(self.http, self.cfg["endpoint"], q)

    @classmethod
    def discover(cls, ctx, canton: str) -> list:
        """Operational stations of the data platform whose coordinates lie in the canton."""
        q = ('{ water { observations { stations(where: {status: {_eq: "Aufgebaut"}}) '
             "{ no name riverName latitude longitude } } } }")
        stations = ctx.cached("bafu-stations", lambda: cls._post(ctx.http, cls.defaults["endpoint"], q)["stations"])
        out = []
        for s in stations:
            lat, lon = s.get("latitude"), s.get("longitude")
            if lat is None or lon is None:
                continue
            try:
                inside = ctx.geo.contains(canton, float(lat), float(lon))
            except Exception as e:
                log.warning("BAFU %s: canton lookup failed (%s)", s.get("no"), e)
                continue
            if inside:
                name = f"{s.get('riverName') or ''} - {s.get('name') or ''} ({s['no']})".strip(" -")
                out.append({"station_id": str(s["no"]), "name": name, "lat": float(lat), "lon": float(lon)})
        return sorted(out, key=lambda s: s["station_id"])

    def _ids(self) -> list:
        return [str(s["station_id"]) for s in self.stations]

    def window(self) -> timedelta:
        """Time window per query so that stations x parameters x 10-minute slots stay below row_cap."""
        rows_per_slot = max(1, len(self.stations)) * max(1, self.cfg["params_per_station"])
        return timedelta(minutes=10 * max(1, self.cfg["row_cap"] // rows_per_slot))

    def history_start(self, st: Optional[dict] = None) -> datetime:
        """From the station metadata (coverageFrom); falls back to history_start from the configuration."""
        try:
            q = ("{ water { observations { stations(where: {no: {_in: %s}}) { no coverageFrom } } } }"
                 % json.dumps([str(st["station_id"])] if st else self._ids()))
            starts = [parse_time(s["coverageFrom"]) for s in self._query(q)["stations"] if s.get("coverageFrom")]
            if starts:
                return min(starts)
        except Exception as e:
            log.debug("BAFU: coverageFrom not available (%s)", e)
        return super().history_start(st)

    def _fetch(self, period: Period) -> Iterator[pd.DataFrame]:
        names = {str(s["station_id"]): s["name"] for s in self.stations}
        ids = json.dumps(list(names))
        seen = set()
        cursor = period.start - timedelta(minutes=10)
        while cursor < period.end:
            nxt = min(cursor + self.window(), period.end + timedelta(minutes=1))
            q = ("{ water { observations { data_10min_mean(where: {station: {no: {_in: %s}}, "
                 "timestamp: {_gte: \"%s\", _lt: \"%s\"}}) "
                 "{ timestamp value parameterName unitSymbol station { no } } } } }"
                 % (ids, cursor.strftime(ISO), nxt.strftime(ISO)))
            try:
                rows = self._query(q)["data_10min_mean"]
            except Exception as e:
                log.warning("BAFU %s–%s: %s", cursor.strftime(ISO), nxt.strftime(ISO), e)
                rows = []
            yield from self._to_frames(rows, names, seen, "interval_start", 10, sid=lambda r: str(r["station"]["no"]))
            cursor = nxt
        if self.cfg["include_live"] and period.end > self.clock() - timedelta(hours=12):
            q = ("{ water { observations { data_live(where: {stationNo: {_in: %s}, timestamp: {_gte: \"%s\"}}) "
                 "{ stationNo parameterName timestamp value } } } }" % (ids, period.start.strftime(ISO)))
            yield from self._to_frames(self._query(q)["data_live"], names, seen, "instant", 0,
                                       sid=lambda r: str(r["stationNo"]))
        for sid in sorted(set(names) - seen):
            log.warning("BAFU %s: no data in the period", sid)

    def _to_frames(self, rows, names, seen, time_ref, interval, sid) -> Iterator[pd.DataFrame]:
        if not rows:
            return
        df = pd.DataFrame({"sid": [sid(r) for r in rows],
                           "param": [r.get("parameterName") for r in rows],
                           "unit": [r.get("unitSymbol") or "" for r in rows],
                           "t": pd.to_datetime([r["timestamp"] for r in rows], utc=True, format="ISO8601"),
                           "v": [r.get("value") for r in rows]})
        for (s, param), g in df.groupby(["sid", "param"]):
            if s not in names:
                continue
            seen.add(s)
            unit = g["unit"].iloc[0] or self.describe(param).unit
            yield self.frame(g["t"], g["v"], s, names[s], param, unit, time_ref, interval)
