from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import FIXED_NOW
from ledwetter.ingest import Ingestor
from ledwetter.model import make_frame
from ledwetter.sources import MeteoSwissSMN, WaPo
from ledwetter.storage import MeasurementStore

UTC = timezone.utc


def frame(times, values, time_ref="interval_end", interval=10, param="air_temperature", sid="A"):
    return make_frame(pd.to_datetime(times, utc=True), source="test", station_id=sid, station_name="Alpha",
                      time_ref=time_ref, interval_min=interval, **{param: values})


@pytest.fixture
def store(tmp_path):
    s = MeasurementStore(tmp_path / "t.db")
    yield s
    s.close()


def epoch(s):
    return int(pd.Timestamp(s, tz="UTC").timestamp())


class TestUpsert:
    def test_idempotent_and_updates_values(self, store):
        store.upsert(frame(["2026-10-03T05:00Z", "2026-10-03T05:10Z"], [1.0, 2.0]))
        store.upsert(frame(["2026-10-03T05:10Z"], [2.5]))
        (s,) = store.series()
        assert store.raw(s["id"], 0, 2**40)["mean"] == [1.0, 2.5]

    def test_series_separated_by_time_ref(self, store):
        store.upsert(frame(["2026-10-03T05:00Z"], [1.0]))
        store.upsert(frame(["2026-10-03T05:00Z"], [1.1], time_ref="instant", interval=0))
        assert len(store.series()) == 2

    def test_series_metadata(self, store):
        store.upsert(frame(["2026-10-03T05:00Z", "2026-10-03T06:00Z"], [1.0, 2.0]))
        (s,) = store.series()
        assert s["first"] == epoch("2026-10-03 05:00") and s["last"] == epoch("2026-10-03 06:00")
        assert s["station_name"] == "Alpha" and s["interval_min"] == 10

    def test_last_time(self, store):
        assert store.last_time("test", "A") is None
        store.upsert(frame(["2026-10-03T05:00Z"], [1.0]))
        store.upsert(frame(["2026-10-03T07:00Z"], [5.0], param="wind_speed"))
        assert store.last_time("test", "A") == datetime(2026, 10, 3, 7, tzinfo=UTC)


class TestAggregate:
    def test_interval_end_values_belong_to_preceding_hour(self, store):
        times = [f"2026-10-03T05:{m:02d}Z" for m in (10, 20, 30, 40, 50)] + ["2026-10-03T06:00Z"]
        store.upsert(frame(times, [1, 2, 3, 4, 5, 6]))
        (s,) = store.series()
        out = store.aggregate(s["id"], epoch("2026-10-03 05:00"), epoch("2026-10-03 06:00"), "hour")
        assert out["t"] == [epoch("2026-10-03 05:00")] and out["mean"] == [3.5] and out["n"] == [6]
        assert out["min"] == [1] and out["max"] == [6] and out["expected"] == 6

    def test_coverage_filter(self, store):
        store.upsert(frame([f"2026-10-03T05:{m:02d}Z" for m in (10, 20, 30, 40)], [1, 2, 3, 4]))
        (s,) = store.series()
        a, b = epoch("2026-10-03 05:00"), epoch("2026-10-03 06:00")
        assert store.aggregate(s["id"], a, b, "hour", 0.75)["t"] == []      # 4 of 6 < 75 %
        assert store.aggregate(s["id"], a, b, "hour", 0.5)["mean"] == [2.5]

    def test_instants_have_no_coverage_filter(self, store):
        store.upsert(frame(["2026-10-03T05:20Z"], [7.0], time_ref="instant", interval=0))
        (s,) = store.series()
        out = store.aggregate(s["id"], epoch("2026-10-03"), epoch("2026-10-04"), "day", 1.0)
        assert out["mean"] == [7.0] and out["expected"] is None

    def test_interval_start_values(self, store):
        store.upsert(frame(["2026-10-03T05:00Z", "2026-10-03T05:50Z", "2026-10-03T06:00Z"], [1, 2, 9],
                           time_ref="interval_start"))
        (s,) = store.series()
        out = store.aggregate(s["id"], epoch("2026-10-03 05:00"), epoch("2026-10-03 07:00"), "hour", 0)
        assert out["mean"] == [1.5, 9.0]


class TestIngestor:
    def test_first_run_starts_at_history_then_incremental(self, store, fake_http, clock):
        src = MeteoSwissSMN(fake_http, {"stations": [{"code": "SMA", "name": "Fluntern"}]}, clock)
        ing = Ingestor(store, [src], clock)
        assert ing.start_for(src, src.stations, None) == datetime(2020, 1, 1, tzinfo=UTC)
        ing.update()
        assert store.last_time("meteoschweiz", "SMA") == FIXED_NOW
        assert ing.start_for(src, src.stations, None) == FIXED_NOW - timedelta(days=2)

    def test_new_station_gets_its_own_history(self, store, fake_http, clock):
        cfg = {"stations": [{"id": "mythenquai", "name": "M"}, {"id": "tiefenbrunnen", "name": "T"}]}
        src = WaPo(fake_http, cfg, clock)
        store.upsert(frame(["2026-10-03T05:00Z"], [1.0]).assign(source="wapo", station_id="mythenquai"))
        ing = Ingestor(store, [src], clock)
        assert ing.start_for(src, [cfg["stations"][0]], None) == datetime(2026, 10, 1, 5, tzinfo=UTC)
        assert ing.start_for(src, [cfg["stations"][1]], None) == datetime(2007, 4, 22, tzinfo=UTC)

    def test_update_survives_failing_source(self, store, clock):
        from conftest import FakeHttp
        src = WaPo(FakeHttp(), {"stations": [{"id": "mythenquai", "name": "M"}]}, clock)
        counts = Ingestor(store, [src], clock).update(since=FIXED_NOW - timedelta(hours=1))
        assert counts == {"wapo:mythenquai": 0}
