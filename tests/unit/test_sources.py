import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import FIXED_NOW, FakeHttp, make_fake_http
from ledwetter.model import Period, concat
from ledwetter.sources import BafuHydro, MeteoSwissSMN, MetarAWC, UgzMeteo, WaPo, build_sources

UTC = timezone.utc
LAST_24H = Period(FIXED_NOW - timedelta(hours=24), FIXED_NOW)


def collect(src, period=LAST_24H):
    return concat(src.fetch(period))


def params(df):
    return set(df["parameter"])


class TestMeteoSwiss:
    def make(self, fake_http, clock, **cfg):
        cfg.setdefault("stations", [{"code": "SMA", "name": "Fluntern"}])
        return MeteoSwissSMN(fake_http, cfg, clock)

    def test_file_selection_last_24h_uses_recent_and_now(self, fake_http, clock):
        files = self.make(fake_http, clock).files_for("SMA", "ogd-smn", LAST_24H)
        assert [f.rsplit("/", 1)[-1] for f in files] == ["ogd-smn_sma_t_recent.csv", "ogd-smn_sma_t_now.csv"]

    def test_file_selection_past_year_uses_historical_only(self, fake_http, clock):
        p = Period(datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 6, 2, tzinfo=UTC))
        files = self.make(fake_http, clock).files_for("SMA", "ogd-smn", p)
        assert [f.rsplit("/", 1)[-1] for f in files] == ["ogd-smn_sma_t_historical_2020-2029.csv"]

    def test_hourly_and_daily_files_are_ignored(self, fake_http, clock):
        files = self.make(fake_http, clock).available_files("SMA", "ogd-smn")
        assert not any("_h_" in f or "_d_" in f for f in files)

    def test_fallback_file_names_without_stac(self, clock):
        src = MeteoSwissSMN(FakeHttp(), {"stations": []}, clock)
        names = [f.rsplit("/", 1)[-1] for f in src.available_files("KLO", "ogd-smn")]
        assert "ogd-smn_klo_t_now.csv" in names and "ogd-smn_klo_t_historical_2020-2029.csv" in names

    def test_all_parameters_extracted_in_native_units(self, fake_http, clock):
        df = collect(self.make(fake_http, clock))
        assert params(df) == {"tre200s0", "tre005s0", "ure200s0", "tde200s0", "prestas0", "rre150z0", "sre000z0",
                              "gre000z0", "dkl010z0", "fu3010z0", "fu3010z1"}  # fkl010z0 duplicate, htoauts0 empty
        units = df.drop_duplicates("parameter").set_index("parameter")["unit"]
        assert units["fu3010z0"] == "km/h" and units["gre000z0"] == "W/m²" and units["dkl010z0"] == "°"

    def test_time_reference_from_code(self, fake_http, clock):
        df = collect(self.make(fake_http, clock)).drop_duplicates("parameter").set_index("parameter")
        assert df.loc["tre200s0", "time_ref"] == "instant" and df.loc["tre200s0", "interval_min"] == 0
        assert df.loc["rre150z0", "time_ref"] == "interval_end" and df.loc["rre150z0", "interval_min"] == 10

    def test_catalogue_from_official_metadata(self, fake_http, clock):
        src = self.make(fake_http, clock)
        collect(src)
        info = src.describe("rre150z0")
        assert info.agg == "sum" and info.unit == "mm" and info.quantity == "precipitation"
        assert info.description == "Niederschlag; Zehnminutensumme"
        assert src.describe("dkl010z0").agg == "dir" and src.describe("fu3010z1").agg == "max"
        assert "rre150h0" not in src.catalog()  # hourly parameters are not part of the 10-minute catalogue

    def test_tower_keeps_kmh_and_drops_ms_duplicate(self, fake_http, clock):
        src = self.make(fake_http, clock, stations=[{"code": "UEB", "name": "Uetliberg", "collection": "ogd-smn-tower"}])
        df = collect(src)
        assert params(df) == {"ta1tows0", "fu3towz0", "dv1towz0", "sre000z0", "gre000z0"}
        assert src.describe("ta1tows0").quantity == "air_temperature"

    def test_parameter_filter_from_config(self, fake_http, clock):
        src = self.make(fake_http, clock, parameters={"include": ["tre*", "rre150z0"], "exclude": ["tre005s0"]})
        assert params(collect(src)) == {"tre200s0", "rre150z0"}

    def test_history_start_from_oldest_file(self, fake_http, clock):
        src = self.make(fake_http, clock)
        assert src.history_start(src.stations[0]) == datetime(2020, 1, 1, tzinfo=UTC)

    def test_missing_metadata_is_tolerated(self, clock, caplog):
        from conftest import fixture_bytes
        http = FakeHttp([(r"items/sma$", lambda m, p: fixture_bytes("smn/stac_ogd-smn_sma.json")),
                         (r"/sma/(ogd-smn_sma_t_now\.csv)$", lambda m, p: fixture_bytes("smn/ogd-smn_sma_t_now.csv"))])
        src = MeteoSwissSMN(http, {"stations": [{"code": "SMA", "name": "F"}]}, clock)
        with caplog.at_level(logging.WARNING):
            df = collect(src, Period(FIXED_NOW - timedelta(hours=2), FIXED_NOW))
        assert "tre200s0" in params(df) and "parameter metadata" in caplog.text
        assert src.describe("rre150z0").agg == "sum"  # fallback from the code


class TestWaPo:
    def test_api_all_fields_local_time_and_units(self, fake_http, clock):
        src = WaPo(fake_http, {"stations": [{"id": "mythenquai", "name": "Mythenquai"}]}, clock)
        df = collect(src)
        assert len(params(df)) == 13 and "water_level" in params(df)
        first = df[df["parameter"] == "air_temperature"]["time_utc"].min()
        assert first == pd.Timestamp(LAST_24H.start)  # 08:00 CEST = 06:00 UTC
        wind = df[df["parameter"] == "wind_speed_avg_10min"]
        assert set(wind["value"]) == {1.5} and set(wind["unit"]) == {"m/s"} and set(wind["time_ref"]) == {"interval_end"}
        assert set(df[df["parameter"] == "air_temperature"]["time_ref"]) == {"unknown"}
        assert src.describe("precipitation").agg == "sum" and src.describe("wind_direction").agg == "dir"

    def test_pagination_stops_on_short_page(self, clock):
        pages = {0: 3, 3: 3, 6: 1}

        def api(m, params):
            n = pages.get(params["offset"], 0)
            return {"result": [{"values": {"timestamp_cet": {"value": "03.10.2026 07:00:00"},
                                           "air_temperature": {"value": 10}}}] * n}
        http = FakeHttp([(r"/measurements/", api)])
        src = WaPo(http, {"stations": [{"id": "mythenquai", "name": "M"}], "page_size": 3}, clock)
        list(src.fetch(LAST_24H))
        assert [c[2]["offset"] for c in http.calls] == [0, 3, 6]

    def test_old_periods_use_ogd_file(self, fake_http, clock):
        src = WaPo(fake_http, {"stations": [{"id": "mythenquai", "name": "M"}]}, clock)
        df = collect(src, Period(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 1, 1, tzinfo=UTC)))
        assert not any("tecdottir" in c[1] for c in fake_http.calls)
        assert len(df[df["parameter"] == "air_temperature"]) == 7 and "_id" not in params(df)
        assert set(df[df["parameter"] == "wind_speed_avg_10min"]["unit"]) == {"m/s"}


class TestBafu:
    def test_windowing_respects_row_cap(self, clock):
        stations = [{"station_id": str(2000 + i), "name": f"S{i}"} for i in range(5)]
        http = FakeHttp(post_handler=lambda p: {"data": {"water": {"observations": {"data_10min_mean": []}}}})
        src = BafuHydro(http, {"stations": stations, "row_cap": 9000}, clock)
        assert src.window() == timedelta(minutes=10 * 600)  # 9000 / (5 stations x 3 parameters)
        list(src.fetch(Period(FIXED_NOW - timedelta(days=10), FIXED_NOW)))
        assert len(http.calls) == 3

    def test_all_parameters_and_missing_station_warning(self, fake_http, clock, caplog):
        src = BafuHydro(fake_http, {"stations": [{"station_id": "2099", "name": "Limmat"},
                                                 {"station_id": "2243", "name": "Unterhard"}]}, clock)
        with caplog.at_level(logging.WARNING):
            df = collect(src)
        assert params(df) == {"WT", "Q", "W"} and set(df["station_id"]) == {"2099"}
        assert set(df[df["parameter"] == "Q"]["unit"]) == {"m³/s"} and (df["time_ref"] == "interval_start").all()
        assert "2243" in caplog.text

    def test_parameter_filter(self, fake_http, clock):
        src = BafuHydro(fake_http, {"stations": [{"station_id": "2099", "name": "L"}], "parameters": {"include": ["WT"]}}, clock)
        assert params(collect(src)) == {"WT"}

    def test_live_feed_optional(self, fake_http, clock):
        src = BafuHydro(fake_http, {"stations": [{"station_id": "2099", "name": "Limmat"}], "include_live": True}, clock)
        assert set(collect(src)["time_ref"]) == {"interval_start", "instant"}

    def test_history_start_from_coverage(self, fake_http, clock):
        src = BafuHydro(fake_http, {"stations": [{"station_id": "2099", "name": "Limmat"}]}, clock)
        assert src.history_start() == datetime(2001, 1, 1, tzinfo=UTC)

    def test_graphql_errors_are_isolated(self, clock, caplog):
        http = FakeHttp(post_handler=lambda p: {"errors": [{"message": "kaputt"}]})
        src = BafuHydro(http, {"stations": [{"station_id": "2099", "name": "L"}]}, clock)
        with caplog.at_level(logging.WARNING):
            assert list(src.fetch(LAST_24H)) == []
        assert "kaputt" in caplog.text


class TestUgz:
    def test_all_parameters_native_units(self, fake_http, clock):
        src = UgzMeteo(fake_http, {"stations": [{"standort": "Zch_Stampfenbachstrasse", "name": "Stampfenbach"}]}, clock)
        df = collect(src)
        assert params(df) == {"T", "Hr", "p", "RainDur", "StrGlo", "WD", "WVs", "WVv"}
        assert set(df[df["parameter"] == "WVs"]["value"]) == {2.5} and set(df[df["parameter"] == "WVs"]["unit"]) == {"m/s"}
        assert (df["interval_min"] == 60).all() and (df["time_ref"] == "interval_start").all()
        assert src.describe("RainDur").agg == "sum" and src.describe("WD").agg == "dir"

    def test_missing_standort_is_reported(self, fake_http, clock, caplog):
        src = UgzMeteo(fake_http, {"stations": [{"standort": "Zch_Heubeeribüel", "name": "Heubeeribüel"}]}, clock)
        with caplog.at_level(logging.WARNING):
            assert collect(src).empty
        assert "Zch_Heubeeribüel" in caplog.text and "Zch_Stampfenbachstrasse" in caplog.text


class TestMetar:
    def test_numeric_fields_native_units(self, fake_http, clock):
        src = MetarAWC(fake_http, {"stations": [{"icao": "LSZH", "name": "Kloten"}]}, clock)
        df = collect(src)
        assert set(df["station_id"]) == {"LSZH"}
        assert params(df) == {"temp", "dewp", "wdir", "wspd", "visib", "altim"}  # wgst/slp empty
        assert set(df[df["parameter"] == "wspd"]["unit"]) == {"kt"} and set(df[df["parameter"] == "visib"]["value"]) == {10.0}
        assert len(df[df["parameter"] == "wdir"]) < len(df[df["parameter"] == "temp"])  # VRB dropped

    def test_hours_capped(self, clock, caplog):
        src = MetarAWC(FakeHttp(), {"stations": [{"icao": "LSZH", "name": "K"}], "max_hours": 48}, clock)
        with caplog.at_level(logging.WARNING):
            h = src.hours_for(Period(FIXED_NOW - timedelta(days=10), FIXED_NOW))
        assert h == 48 and "archive" in caplog.text


def test_registry_rejects_unknown_source(fake_http):
    with pytest.raises(ValueError):
        build_sources({}, fake_http, ["meteoschweiz", "netatmo"])


def test_one_failing_station_does_not_stop_others(clock):
    src = MeteoSwissSMN(make_fake_http(), {"stations": [{"code": "XXX", "name": "fehlt"}, {"code": "SMA", "name": "Fluntern"}]}, clock)
    assert set(collect(src)["station_id"]) == {"SMA"}
