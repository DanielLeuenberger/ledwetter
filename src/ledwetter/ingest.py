"""Building and updating the local database."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from .model import Clock, Period, utcnow
from .storage import MeasurementStore

log = logging.getLogger(__name__)


class Ingestor:
    """Loads data for each station from its latest stored measurement time (minus an overlap),
    or from the start of the available history if the station is not yet in the database.
    The parameter catalogue of each source is stored alongside the measurements."""

    def __init__(self, store: MeasurementStore, sources: list, clock: Clock = utcnow,
                 overlap: timedelta = timedelta(days=2)):
        self.store, self.sources, self.clock, self.overlap = store, sources, clock, overlap

    def start_for(self, src, stations: list, since: Optional[datetime]) -> datetime:
        if since is not None:
            return since
        starts = []
        for st in stations:
            last = self.store.last_time(src.name, src.station_key(st))
            starts.append(last - self.overlap if last else src.history_start(st))
        return min(starts)

    def update(self, since: Optional[datetime] = None) -> dict:
        now = self.clock()
        counts: dict = {}
        for src in self.sources:
            groups = [[st] for st in src.stations] if src.per_station else ([src.stations] if src.stations else [])
            for stations in groups:
                sub = src.with_stations(stations)
                try:
                    start = self.start_for(sub, stations, since)
                except Exception as e:
                    log.warning("%s: start time could not be determined (%s)", src.name, e)
                    continue
                if start >= now:
                    continue
                for st in stations:
                    self.store.set_station_meta(src.name, sub.station_key(st), st.get("name"),
                                                st.get("canton") or src.cfg.get("canton"),
                                                st.get("lat"), st.get("lon"), st.get("height_masl"))
                label = ",".join(sub.station_key(s) for s in stations)
                log.info("%s %s: loading from %s", src.name, label, start.strftime("%Y-%m-%d %H:%M"))
                n = 0
                try:
                    for df in sub.fetch(Period(start, now)):
                        n += self.store.upsert(df, sub.describe)
                except Exception:
                    log.exception("%s %s: aborted", src.name, label)
                counts[f"{src.name}:{label}"] = n
                log.info("%s %s: %d values stored", src.name, label, n)
        return counts
