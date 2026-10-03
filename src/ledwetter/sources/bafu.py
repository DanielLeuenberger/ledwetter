"""FOEN (BAFU) hydrology via the data platform data.bafu.admin.ch (GraphQL, no API key).

Water temperature (parameter WT) as 10-minute means; timestamp = interval start (UTC).
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
                "history_start": "1990-01-01"}

    def _query(self, q: str) -> dict:
        payload = self.http.post_json(self.cfg["endpoint"], {"query": q})
        if "errors" in payload:
            raise RuntimeError(payload["errors"])
        return payload["data"]["water"]["observations"]

    def _ids(self) -> list:
        return [str(s["station_id"]) for s in self.stations]

    def window(self) -> timedelta:
        """Time window per query so that stations x 10-minute slots stay below row_cap."""
        return timedelta(minutes=10 * max(1, self.cfg["row_cap"] // max(1, len(self.stations))))

    def history_start(self, st: Optional[dict] = None) -> datetime:
        """From the station metadata (coverageFrom); falls back to history_start from the configuration."""
        try:
            q = ("{ water { observations { stations(where: {no: {_in: %s}}) { no coverageFrom } } } }"
                 % json.dumps([str(st["station_id"])] if st else self._ids()))
            starts = [parse_time(s["coverageFrom"]) for s in self._query(q)["stations"] if s.get("coverageFrom")]
            if starts:
                return min(starts)
        except Exception as e:
            log.debug("BAFU: coverageFrom nicht verfügbar (%s)", e)
        return super().history_start(st)

    def _fetch(self, period: Period) -> Iterator[pd.DataFrame]:
        names = {str(s["station_id"]): s["name"] for s in self.stations}
        ids = json.dumps(list(names))
        seen = set()
        cursor = period.start - timedelta(minutes=10)
        while cursor < period.end:
            nxt = min(cursor + self.window(), period.end + timedelta(minutes=1))
            q = ("{ water { observations { data_10min_mean(where: {station: {no: {_in: %s}}, "
                 "parameterName: {_eq: \"WT\"}, timestamp: {_gte: \"%s\", _lt: \"%s\"}}) "
                 "{ timestamp value station { no } } } } }" % (ids, cursor.strftime(ISO), nxt.strftime(ISO)))
            try:
                rows = self._query(q)["data_10min_mean"]
            except Exception as e:
                log.warning("BAFU %s–%s: %s", cursor.strftime(ISO), nxt.strftime(ISO), e)
                rows = []
            yield from self._to_frames(rows, names, seen, "interval_start", 10,
                                       sid=lambda r: str(r["station"]["no"]))
            cursor = nxt
        if self.cfg["include_live"] and period.end > self.clock() - timedelta(hours=12):
            q = ("{ water { observations { data_live(where: {stationNo: {_in: %s}, parameterName: {_eq: \"WT\"}, "
                 "timestamp: {_gte: \"%s\"}}) { stationNo timestamp value } } } }" % (ids, period.start.strftime(ISO)))
            yield from self._to_frames(self._query(q)["data_live"], names, seen, "instant", 0,
                                       sid=lambda r: str(r["stationNo"]))
        for sid in sorted(set(names) - seen):
            log.warning("BAFU %s: keine Wassertemperatur im Zeitraum", sid)

    def _to_frames(self, rows, names, seen, time_ref, interval, sid) -> Iterator[pd.DataFrame]:
        if not rows:
            return
        df = pd.DataFrame({"sid": [sid(r) for r in rows],
                           "t": pd.to_datetime([r["timestamp"] for r in rows], utc=True, format="ISO8601"),
                           "v": [r.get("value") for r in rows]})
        for s, g in df.groupby("sid"):
            if s not in names:
                continue
            seen.add(s)
            yield self.frame(g["t"], time_ref, interval, s, names[s], water_temperature=g["v"])
