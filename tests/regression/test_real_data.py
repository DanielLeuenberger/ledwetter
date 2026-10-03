"""Parsers against real responses recorded in a diagnostic run (tests/fixtures/real, 3 October 2026)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from conftest import FakeHttp
from ledwetter.discover import StationDiscovery
from ledwetter.geo import CantonGeo
from ledwetter.model import Period, concat
from ledwetter.sources import BafuHydro, MeteoSwissSMN, MetarAWC, UgzMeteo, WaPo

REAL = Path(__file__).parents[1] / "fixtures" / "real"
RECORDED = datetime(2026, 10, 3, 12, 17, 8, tzinfo=timezone.utc)
UTC = timezone.utc


def real(name: str) -> bytes:
    return (REAL / name).read_bytes()


def clock():
    return RECORDED


def smn_http() -> FakeHttp:
    return FakeHttp([(r"/ch\.meteoschweiz\.([\w-]+)/(\1_meta_\w+\.csv)$", lambda m, p: real(m.group(2)))])


def smn_frames(code, coll, fname):
    src = MeteoSwissSMN(smn_http(), {"stations": []}, clock)
    text = real(fname).decode("cp1252")
    return src, concat(src.parse(text, {"code": code, "name": code}, coll))


class TestMeteoSwiss:
    def test_official_parameter_list_gives_aggregation_methods(self):
        src = MeteoSwissSMN(smn_http(), {"stations": []}, clock)
        cat = src.collection_catalog("ogd-smn")
        assert len(cat) > 25 and all(info.code == code for code, info in cat.items())
        assert {c: cat[c].agg for c in ("rre150z0", "sre000z0", "dkl010z0", "fu3010z1", "fu3010z3", "tre200s0", "wcc006s0")} == {
            "rre150z0": "sum", "sre000z0": "sum", "dkl010z0": "dir", "fu3010z1": "max", "fu3010z3": "max",
            "tre200s0": "mean", "wcc006s0": "max"}
        assert cat["gre000z0"].unit == "W/m²" and cat["tre200s0"].unit == "°C"

    def test_weather_station_file(self):
        src, df = smn_frames("SMA", "ogd-smn", "ogd-smn_sma_t_now.csv")
        params = set(df["parameter"])
        assert {"tre200s0", "rre150z0", "fu3010z0", "dkl010z0", "gre000z0"} <= params
        assert not any(p.startswith("fkl010") for p in params)  # m/s duplicates of the km/h columns
        t = df.drop_duplicates("parameter").set_index("parameter")
        assert t.loc["tre200s0", "time_ref"] == "instant" and t.loc["rre150z0", "time_ref"] == "interval_end"
        assert df["time_utc"].min() == pd.Timestamp("2026-10-03 00:00", tz="UTC")  # 'now' starts today 00:00 UTC

    def test_tower_file_keeps_kmh_and_both_ms_spellings_are_dropped(self):
        src, df = smn_frames("UEB", "ogd-smn-tower", "ogd-smn-tower_ueb_t_now.csv")
        assert set(df["parameter"]) == {"ta1tows0", "tdetows0", "uretows0", "dv1towz0", "fu3towz0", "fu3towz1",
                                        "gre000z0", "sre000z0"}
        units = df.drop_duplicates("parameter").set_index("parameter")["unit"]
        assert units["fu3towz1"] == "km/h" and units["ta1tows0"] == "°C"

    def test_precipitation_station_file(self):
        src, df = smn_frames("AFI", "ogd-smn-precip", "ogd-smn-precip_afi_t_now.csv")
        assert set(df["parameter"]) == {"rre150z0"}

    def test_station_lists_of_canton_zurich(self):
        http = FakeHttp([(r"/ch\.meteoschweiz\.([\w-]+)/(\1_meta_stations\.csv)$", lambda m, p: real(m.group(2)))])
        found = MeteoSwissSMN.discover(StationDiscovery(http, "ZH", clock).ctx, "ZH")
        by_coll = {}
        for s in found:
            by_coll.setdefault(s.get("collection", "ogd-smn"), []).append(s["code"])
        assert sorted(by_coll["ogd-smn"]) == ["HOE", "KLO", "LAE", "PFA", "REH", "SMA", "UEB", "WAE"]
        assert by_coll["ogd-smn-tower"] == ["UEB"]
        assert sorted(by_coll["ogd-smn-precip"]) == ["AFI", "BUE", "DIT", "HIW", "KUE", "LGA", "OPF", "UST", "WAG", "WIN", "ZWK"]


def test_water_police_uses_utc_timestamp_consistent_with_local_time():
    http = FakeHttp([(r"/measurements/tiefenbrunnen", lambda m, p: real("tecdottir_tiefenbrunnen.json") if not p.get("offset") else {"result": []})])
    src = WaPo(http, {"stations": [{"id": "tiefenbrunnen", "name": "T"}]}, clock)
    df = concat(src.fetch(Period(RECORDED - timedelta(hours=20), RECORDED)))
    items = json.loads(real("tecdottir_tiefenbrunnen.json"))["result"]
    expected = sorted(pd.Timestamp(i["values"]["timestamp_cet"]["value"]).tz_convert("UTC") for i in items)
    assert sorted(df[df["parameter"] == "air_temperature"]["time_utc"]) == expected
    assert df[df["parameter"] == "air_temperature"]["time_utc"].min() == pd.Timestamp("2026-10-02 22:00", tz="UTC")


def test_geo_services():
    http = FakeHttp([(r"/MapServer/find", lambda m, p: real("find_zh_without_geometry.json")),
                     (r"/MapServer/identify", lambda m, p: real("identify_zh.json"))])
    geo = CantonGeo(http)
    lat0, lat1, lon0, lon1 = geo.bbox("ZH")
    assert (round(lat0, 2), round(lat1, 2), round(lon0, 2), round(lon1, 2)) == (47.16, 47.69, 8.35, 9.0)
    assert geo.canton_of(47.38, 8.54) == "ZH"


def test_metar_station_info_and_reports():
    http = FakeHttp([(r"stationinfo", lambda m, p: real("metar_stationinfo.json")),
                     (r"/MapServer/find", lambda m, p: real("find_zh_without_geometry.json")),
                     (r"/MapServer/identify", lambda m, p: real("identify_zh.json")),
                     (r"data/metar", lambda m, p: real("metar_lszh_lsmd.json"))])
    stations = MetarAWC.discover(StationDiscovery(http, "ZH", clock).ctx, "ZH")
    assert [s["icao"] for s in stations] == ["LSMD", "LSZH"]
    src = MetarAWC(http, {"stations": stations}, clock)
    df = concat(src.fetch(Period(RECORDED - timedelta(hours=15), RECORDED)))
    reports = pd.DataFrame(json.loads(real("metar_lszh_lsmd.json")))
    reports["t"] = pd.to_datetime(reports["obsTime"], unit="s", utc=True)
    no_direction = reports[(reports["wspd"] == 0) | (reports["wdir"].astype(str) == "VRB")]
    stored = df[df["parameter"] == "wdir"]
    assert len(no_direction) >= 20  # calm and variable wind are frequent in this recording
    assert not set(zip(stored["station_id"], stored["time_utc"])) & set(zip(no_direction["icaoId"], no_direction["t"]))
    assert (stored["value"] > 0).all()
    vis = df[(df["station_id"] == "LSZH") & (df["parameter"] == "visib")]["value"]
    assert set(vis) == {10.0, 9.0} and set(df[df["parameter"] == "visib"]["unit"]) == {"km"}


def test_bafu_data_units():
    rows = json.loads(real("bafu_data_10min_mean.json"))
    http = FakeHttp(post_handler=lambda p: rows)
    ids = sorted({r["station"]["no"] for r in rows["data"]["water"]["observations"]["data_10min_mean"]})
    src = BafuHydro(http, {"stations": [{"station_id": i, "name": i} for i in ids]}, clock)
    df = concat(src.fetch(Period(datetime(2026, 10, 3, 9, tzinfo=UTC), datetime(2026, 10, 3, 11, tzinfo=UTC))))
    units = df.drop_duplicates("parameter").set_index("parameter")["unit"].to_dict()
    assert units == {"Q": "m3/s", "W": "m ü.M.", "WT": "°C"} and (df["time_ref"] == "interval_start").all()


def test_ugz_file_format_and_time_reference():
    http = FakeHttp([(r"ugz_ogd_meteo_h1_2026", lambda m, p: real("ugz_ogd_meteo_h1_2026_head.csv"))])
    src = UgzMeteo(http, {"stations": [{"standort": "Zch_Heubeeribüel", "name": "Heubeeribüel"}]}, clock)
    df = concat(src.fetch(Period(datetime(2025, 12, 31, 22, tzinfo=UTC), datetime(2026, 1, 1, 6, tzinfo=UTC))))
    assert set(df["parameter"]) <= {"T", "Hr", "p"} and (df["time_ref"] == "interval_start").all()
    assert df["time_utc"].min() == pd.Timestamp("2025-12-31 23:00", tz="UTC")  # 2026-01-01T00:00+0100
