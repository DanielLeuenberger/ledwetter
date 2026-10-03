from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import FIXED_NOW, FakeHttp
from ledwetter.ingest import Ingestor
from ledwetter.model import ParameterInfo, make_frame
from ledwetter.sources import MeteoSwissSMN, WaPo
from ledwetter.storage import MeasurementStore, SchemaError

UTC = timezone.utc


def frame(times, values, time_ref="interval_end", interval=10, param="p1", sid="A", unit="°C"):
    return make_frame(pd.to_datetime(times, utc=True), values, source="test", station_id=sid, station_name="Alpha",
                      parameter=param, unit=unit, time_ref=time_ref, interval_min=interval)


def describe_as(agg):
    return lambda code, unit: ParameterInfo("test", code, f"desc {code}", unit, "G", agg, None)


@pytest.fixture
def store(tmp_path):
    s = MeasurementStore(tmp_path / "t.db")
    yield s
    s.close()


def epoch(s):
    return int(pd.Timestamp(s, tz="UTC").timestamp())


HOUR = [f"2026-10-03T05:{m:02d}Z" for m in (10, 20, 30, 40, 50)] + ["2026-10-03T06:00Z"]
A, B = epoch("2026-10-03 05:00"), epoch("2026-10-03 06:00")


class TestUpsert:
    def test_idempotent_and_updates_values(self, store):
        store.upsert(frame(["2026-10-03T05:00Z", "2026-10-03T05:10Z"], [1.0, 2.0]))
        store.upsert(frame(["2026-10-03T05:10Z"], [2.5]))
        (s,) = store.series()
        assert store.raw(s["id"], 0, 2**40)["value"] == [1.0, 2.5]

    def test_series_separated_by_time_ref(self, store):
        store.upsert(frame(["2026-10-03T05:00Z"], [1.0]))
        store.upsert(frame(["2026-10-03T05:00Z"], [1.1], time_ref="instant", interval=0))
        assert len(store.series()) == 2

    def test_catalogue_is_stored_with_series(self, store):
        store.upsert(frame(["2026-10-03T05:00Z", "2026-10-03T06:00Z"], [1.0, 2.0]), describe_as("sum"))
        (s,) = store.series()
        assert s["first"] == epoch("2026-10-03 05:00") and s["last"] == epoch("2026-10-03 06:00")
        assert s["description"] == "desc p1" and s["agg"] == "sum" and s["unit"] == "°C" and s["group"] == "G"

    def test_last_time(self, store):
        assert store.last_time("test", "A") is None
        store.upsert(frame(["2026-10-03T05:00Z"], [1.0]))
        store.upsert(frame(["2026-10-03T07:00Z"], [5.0], param="p2"))
        assert store.last_time("test", "A") == datetime(2026, 10, 3, 7, tzinfo=UTC)

    def test_old_schema_is_rejected(self, tmp_path):
        import sqlite3
        path = tmp_path / "old.db"
        sqlite3.connect(path).execute("CREATE TABLE series (id INTEGER PRIMARY KEY, medium TEXT)")
        with pytest.raises(SchemaError):
            MeasurementStore(path)


    def test_migration_from_version_2_keeps_data(self, tmp_path):
        import sqlite3
        path = tmp_path / "v2.db"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE stations (id INTEGER PRIMARY KEY, source TEXT NOT NULL, station_id TEXT NOT NULL,
                                   name TEXT NOT NULL, UNIQUE (source, station_id));
            CREATE TABLE series (id INTEGER PRIMARY KEY, station INTEGER, parameter TEXT, unit TEXT, time_ref TEXT,
                                 interval_min INTEGER, UNIQUE (station, parameter, time_ref, interval_min));
            CREATE TABLE measurements (series INTEGER, t INTEGER, value REAL, PRIMARY KEY (series, t)) WITHOUT ROWID;
            INSERT INTO stations VALUES (1, 'test', 'A', 'Alpha');
            INSERT INTO series VALUES (1, 1, 'p1', '°C', 'instant', 0);
            INSERT INTO measurements VALUES (1, 1791007800, 4.2);
            PRAGMA user_version = 2;""")
        conn.close()
        store = MeasurementStore(path)
        (s,) = store.series()
        assert s["canton"] is None and store.raw(s["id"], 0, 2**40)["value"] == [4.2]
        store.set_station_meta("test", "A", "Alpha", canton="ZH", lat=47.4, lon=8.5)
        assert store.series()[0]["canton"] == "ZH"
        assert store.conn.execute("PRAGMA user_version").fetchone()[0] == 3
        store.close()

    def test_station_meta_keeps_existing_values(self, store):
        store.set_station_meta("test", "A", "Alpha", canton="ZH", lat=47.4, lon=8.5, height_masl=500)
        store.set_station_meta("test", "A", "Alpha neu")
        store.upsert(frame(["2026-10-03T05:00Z"], [1.0]))
        (s,) = store.series()
        assert (s["canton"], s["lat"], s["height_masl"], s["station_name"]) == ("ZH", 47.4, 500, "Alpha")


class TestAggregate:
    def test_mean_with_band_and_interval_end_binning(self, store):
        store.upsert(frame(HOUR, [1, 2, 3, 4, 5, 6]), describe_as("mean"))
        (s,) = store.series()
        out = store.aggregate(s["id"], A, B, "hour")
        assert out["t"] == [A] and out["value"] == [3.5] and out["n"] == [6]
        assert out["min"] == [1] and out["max"] == [6] and out["expected"] == 6 and out["agg"] == "mean"

    def test_sum_without_band(self, store):
        store.upsert(frame(HOUR, [0.2, 0, 0, 0.4, 0, 0.1], param="rain", unit="mm"), describe_as("sum"))
        (s,) = store.series()
        out = store.aggregate(s["id"], A, B, "hour")
        assert out["value"] == [pytest.approx(0.7)] and out["min"] is None and out["agg"] == "sum"

    def test_max(self, store):
        store.upsert(frame(HOUR, [10, 30, 12, 8, 25, 9], param="gust"), describe_as("max"))
        (s,) = store.series()
        assert store.aggregate(s["id"], A, B, "hour")["value"] == [30]

    def test_direction_vector_mean_across_north(self, store):
        store.upsert(frame(HOUR, [350, 10, 350, 10, 350, 10], param="dir", unit="°"), describe_as("dir"))
        (s,) = store.series()
        out = store.aggregate(s["id"], A, B, "hour")
        assert out["value"][0] == pytest.approx(0, abs=1e-6) or out["value"][0] == pytest.approx(360, abs=1e-6)
        assert out["n"] == [6]  # an arithmetic mean would wrongly give 180°

    def test_coverage_filter(self, store):
        store.upsert(frame(HOUR[:4], [1, 2, 3, 4]))
        (s,) = store.series()
        assert store.aggregate(s["id"], A, B, "hour", 0.75)["t"] == []      # 4 of 6 < 75 %
        assert store.aggregate(s["id"], A, B, "hour", 0.5)["value"] == [2.5]

    def test_instants_have_no_coverage_filter(self, store):
        store.upsert(frame(["2026-10-03T05:20Z"], [7.0], time_ref="instant", interval=0))
        (s,) = store.series()
        out = store.aggregate(s["id"], epoch("2026-10-03"), epoch("2026-10-04"), "day", 1.0)
        assert out["value"] == [7.0] and out["expected"] is None

    def test_interval_start_values(self, store):
        store.upsert(frame(["2026-10-03T05:00Z", "2026-10-03T05:50Z", "2026-10-03T06:00Z"], [1, 2, 9],
                           time_ref="interval_start"))
        (s,) = store.series()
        assert store.aggregate(s["id"], A, epoch("2026-10-03 07:00"), "hour", 0)["value"] == [1.5, 9.0]


class TestIngestor:
    def test_first_run_starts_at_history_then_incremental(self, store, fake_http, clock):
        src = MeteoSwissSMN(fake_http, {"stations": [{"code": "SMA", "name": "Fluntern"}]}, clock)
        ing = Ingestor(store, [src], clock)
        assert ing.start_for(src, src.stations, None) == datetime(2020, 1, 1, tzinfo=UTC)
        ing.update()
        assert store.last_time("meteoschweiz", "SMA") == FIXED_NOW
        assert ing.start_for(src, src.stations, None) == FIXED_NOW - timedelta(days=2)
        rain = next(s for s in store.series() if s["parameter"] == "rre150z0")
        assert rain["agg"] == "sum" and rain["quantity"] == "precipitation" and rain["unit"] == "mm"

    def test_station_metadata_from_configuration(self, store, fake_http, clock):
        stations = [{"code": "SMA", "name": "Fluntern", "canton": "ZH", "lat": 47.38, "lon": 8.57, "height_masl": 556}]
        src = MeteoSwissSMN(fake_http, {"stations": stations}, clock)
        Ingestor(store, [src], clock).update(since=FIXED_NOW - timedelta(hours=1))
        s = store.series()[0]
        assert (s["canton"], s["lat"], s["height_masl"]) == ("ZH", 47.38, 556)

    def test_new_station_gets_its_own_history(self, store, fake_http, clock):
        cfg = {"stations": [{"id": "mythenquai", "name": "M"}, {"id": "tiefenbrunnen", "name": "T"}]}
        src = WaPo(fake_http, cfg, clock)
        store.upsert(frame(["2026-10-03T05:00Z"], [1.0]).assign(source="wapo", station_id="mythenquai"))
        ing = Ingestor(store, [src], clock)
        assert ing.start_for(src, [cfg["stations"][0]], None) == datetime(2026, 10, 1, 5, tzinfo=UTC)
        assert ing.start_for(src, [cfg["stations"][1]], None) == datetime(2007, 4, 22, tzinfo=UTC)

    def test_update_survives_failing_source(self, store, clock):
        src = WaPo(FakeHttp(), {"stations": [{"id": "mythenquai", "name": "M"}]}, clock)
        counts = Ingestor(store, [src], clock).update(since=FIXED_NOW - timedelta(hours=1))
        assert counts == {"wapo:mythenquai": 0}
