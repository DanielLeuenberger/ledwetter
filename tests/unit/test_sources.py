import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import FIXED_NOW, FIXTURES, FakeHttp, make_fake_http
from ledwetter.model import Period
from ledwetter.sources import BafuHydro, MeteoSwissSMN, MetarAWC, UgzMeteo, WaPo, build_sources

UTC = timezone.utc
LAST_24H = Period(FIXED_NOW - timedelta(hours=24), FIXED_NOW)


def collect(src, period=LAST_24H):
    frames = list(src.fetch(period))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


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

    def test_values_units_and_time_reference(self, fake_http, clock):
        df = collect(self.make(fake_http, clock))
        assert set(df["parameter"]) == {"air_temperature", "wind_speed"}
        assert (df["time_ref"] == "interval_end").all() and (df["interval_min"] == 10).all()
        assert df["time_utc"].min() >= LAST_24H.start and df["time_utc"].max() <= LAST_24H.end

    def test_tower_wind_in_ms_is_converted(self, fake_http, clock):
        src = self.make(fake_http, clock, stations=[{"code": "UEB", "name": "Uetliberg", "collection": "ogd-smn-tower"}])
        df = collect(src)
        wind = df[df["parameter"] == "wind_speed"]["value"]
        assert not wind.empty and wind.between(5, 12).all()  # fixture: 6–10 km/h stored as m/s

    def test_unknown_columns_are_logged(self, fake_http, clock, caplog):
        src = self.make(fake_http, clock)
        text = "station_abbr;reference_timestamp;xyz\nSMA;03.10.2026 05:00;1\n"
        with caplog.at_level(logging.WARNING):
            out = src.parse(text, {"code": "SMA", "name": "x"}, src.cfg["params"]["ogd-smn"])
        assert out.empty and "keine bekannten Parameter" in caplog.text

    def test_history_start_from_oldest_file(self, fake_http, clock):
        src = self.make(fake_http, clock)
        assert src.history_start(src.stations[0]) == datetime(2020, 1, 1, tzinfo=UTC)


class TestWaPo:
    def test_api_local_time_is_converted_to_utc(self, fake_http, clock):
        src = WaPo(fake_http, {"stations": [{"id": "mythenquai", "name": "Mythenquai"}]}, clock)
        df = collect(src)
        first = df[df["parameter"] == "air_temperature"]["time_utc"].min()
        assert first == pd.Timestamp(LAST_24H.start)  # 08:00 CEST = 06:00 UTC
        assert set(df[df["parameter"] == "water_temperature"]["time_ref"]) == {"instant"}
        assert set(df[df["parameter"] == "wind_speed"]["value"]) == {5.4}  # 1.5 m/s

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
        p = Period(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 1, 1, tzinfo=UTC))
        df = collect(src, p)
        assert not any("tecdottir" in c[1] for c in fake_http.calls)
        assert len(df[df["parameter"] == "air_temperature"]) == 7
        assert set(df[df["parameter"] == "wind_speed"]["value"]) == {7.2}


class TestBafu:
    def test_windowing_respects_row_cap(self, clock):
        stations = [{"station_id": str(2000 + i), "name": f"S{i}"} for i in range(5)]
        http = FakeHttp(post_handler=lambda p: {"data": {"water": {"observations": {"data_10min_mean": []}}}})
        src = BafuHydro(http, {"stations": stations, "row_cap": 9000}, clock)
        assert src.window() == timedelta(minutes=10 * 1800)
        list(src.fetch(Period(FIXED_NOW - timedelta(days=30), FIXED_NOW)))
        assert len(http.calls) == 3  # 30 days / 12.5 days per window

    def test_values_and_missing_station_warning(self, fake_http, clock, caplog):
        src = BafuHydro(fake_http, {"stations": [{"station_id": "2099", "name": "Limmat"},
                                                 {"station_id": "2243", "name": "Unterhard"}]}, clock)
        with caplog.at_level(logging.WARNING):
            df = collect(src)
        assert set(df["station_id"]) == {"2099"} and (df["time_ref"] == "interval_start").all()
        assert "2243" in caplog.text

    def test_live_feed_optional(self, fake_http, clock):
        src = BafuHydro(fake_http, {"stations": [{"station_id": "2099", "name": "Limmat"}], "include_live": True}, clock)
        df = collect(src)
        assert set(df["time_ref"]) == {"interval_start", "instant"}

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
    def test_first_wind_parameter_only_and_unit_conversion(self, fake_http, clock):
        src = UgzMeteo(fake_http, {"stations": [{"standort": "Zch_Stampfenbachstrasse", "name": "Stampfenbach"}]}, clock)
        df = collect(src)
        assert set(df["parameter"]) == {"air_temperature", "wind_speed"}
        assert set(df[df["parameter"] == "wind_speed"]["value"]) == {9.0}  # WVs 2.5 m/s, not WVv
        assert (df["interval_min"] == 60).all() and (df["time_ref"] == "unknown").all()

    def test_missing_standort_is_reported(self, fake_http, clock, caplog):
        src = UgzMeteo(fake_http, {"stations": [{"standort": "Zch_Kaserne", "name": "Kaserne"}]}, clock)
        with caplog.at_level(logging.WARNING):
            assert collect(src).empty
        assert "Zch_Kaserne" in caplog.text and "Zch_Stampfenbachstrasse" in caplog.text


class TestMetar:
    def test_filters_unconfigured_stations_and_converts_knots(self, fake_http, clock):
        src = MetarAWC(fake_http, {"stations": [{"icao": "LSZH", "name": "Kloten"}]}, clock)
        df = collect(src)
        assert set(df["station_id"]) == {"LSZH"}
        assert df[df["parameter"] == "wind_speed"]["value"].tolist() == pytest.approx([11.112] * 48)  # 05:50 on the previous day is outside the period

    def test_hours_capped(self, clock, caplog):
        src = MetarAWC(FakeHttp(), {"stations": [{"icao": "LSZH", "name": "K"}], "max_hours": 48}, clock)
        with caplog.at_level(logging.WARNING):
            h = src.hours_for(Period(FIXED_NOW - timedelta(days=10), FIXED_NOW))
        assert h == 48 and "Archiv" in caplog.text


def test_registry_rejects_unknown_source(fake_http):
    with pytest.raises(ValueError):
        build_sources({}, fake_http, ["meteoschweiz", "netatmo"])


def test_one_failing_station_does_not_stop_others(clock):
    http = make_fake_http()
    src = MeteoSwissSMN(http, {"stations": [{"code": "XXX", "name": "fehlt"}, {"code": "SMA", "name": "Fluntern"}]}, clock)
    df = collect(src)
    assert set(df["station_id"]) == {"SMA"}
