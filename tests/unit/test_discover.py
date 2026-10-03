import pytest
import yaml

from conftest import make_fake_http
from ledwetter.discover import StationDiscovery
from ledwetter.geo import CantonGeo, parse_cantons
from ledwetter.sources import MeteoSwissSMN, Source


def discovery(clock, cantons="ZH"):
    return StationDiscovery(make_fake_http(), cantons, clock)


def ids(cfg, section, key):
    return [s[key] for s in cfg.get(section, {}).get("stations", [])]


def test_parse_cantons():
    assert parse_cantons("zh, be,ZH") == ["ZH", "BE"]
    with pytest.raises(ValueError):
        parse_cantons("ZH,XX")


def test_canton_geo_bbox_and_lookup():
    geo = CantonGeo(make_fake_http())
    assert geo.bbox("ZH") == (47.15, 47.70, 8.35, 9.0) and geo.bbox("BE") is None
    assert geo.contains("ZH", 47.39, 8.53) and not geo.contains("ZH", 47.48, 8.19)
    assert geo.contains("BE", 46.9, 7.5)
    calls = len([c for c in geo.http.calls if "identify" in c[1]])
    geo.contains("ZH", 47.39, 8.53)
    assert len([c for c in geo.http.calls if "identify" in c[1]]) == calls  # cached


def test_zurich(clock):
    cfg = discovery(clock).build_config()
    assert sorted(ids(cfg, "meteoschweiz", "code")) == ["HOE", "KLO", "LAE", "PFA", "REH", "TST", "UEB"]
    assert ids(cfg, "bafu", "station_id") == ["2099", "2176"]
    assert ids(cfg, "metar", "icao") == ["LSMD", "LSZH"]  # TAF-only and foreign stations are skipped
    assert ids(cfg, "wapo", "id") == ["tiefenbrunnen", "mythenquai"]
    assert ids(cfg, "ugz", "standort") == ["Zch_Schimmelstrasse", "Zch_Stampfenbachstrasse"]
    assert {s["canton"] for sec in ("meteoschweiz", "bafu", "metar", "ugz") for s in cfg[sec]["stations"]} == {"ZH"}


def test_bern_only_national_sources(clock):
    cfg = discovery(clock, "BE").build_config()
    assert set(cfg) == {"settings", "meteoschweiz", "metar"}  # no BAFU station in BE in the fixtures
    assert sorted(ids(cfg, "meteoschweiz", "code")) == ["ABO", "BAN"]
    assert ids(cfg, "metar", "icao") == ["LSZB"]


def test_canton_without_bbox_checks_every_station(clock):
    d = discovery(clock, "AG")
    cfg = d.build_config()
    assert ids(cfg, "bafu", "station_id") == ["2018", "2500"]
    looked_up = {c[2]["geometry"] for c in d.ctx.http.calls if "identify" in c[1]}
    assert len(looked_up) >= 4  # no bounding box for AG -> every station with coordinates is looked up


def test_several_cantons_are_merged_and_lists_downloaded_once(clock):
    d = discovery(clock, ["ZH", "BE"])
    cfg = d.build_config()
    assert sorted(ids(cfg, "metar", "icao")) == ["LSMD", "LSZB", "LSZH"]
    cantons = {s["code"]: s["canton"] for s in cfg["meteoschweiz"]["stations"]}
    assert cantons["KLO"] == "ZH" and cantons["ABO"] == "BE"
    smn_lists = [c for c in d.ctx.http.calls if c[1].endswith("ogd-smn_meta_stations.csv")]
    assert len(smn_lists) == 1


def test_regional_source_extension(clock):
    class CityNetwork(Source):
        name, key, regions = "citynet", "id", ("BE",)

        @classmethod
        def discover(cls, ctx, canton):
            return [{"id": "b1", "name": "Bern Bärenplatz"}]

    sources = {"meteoschweiz": MeteoSwissSMN, "citynet": CityNetwork}
    assert "citynet" not in StationDiscovery(make_fake_http(), "ZH", clock, sources).build_config()
    cfg = StationDiscovery(make_fake_http(), "BE", clock, sources).build_config()
    assert cfg["citynet"]["stations"] == [{"id": "b1", "name": "Bern Bärenplatz", "canton": "BE"}]


def test_written_configuration_is_usable(tmp_path, clock):
    path = tmp_path / "zh.yaml"
    discovery(clock).write(str(path), {"db_file": "x.db"})
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert cfg["settings"] == {"db_file": "x.db"}
    assert {"meteoschweiz", "wapo", "bafu", "ugz", "metar"} <= set(cfg)
