"""Generates the synthetic test fixtures (run once; the output is committed).

The formats follow the documentation of each source. Real excerpts:
- smn/real_zermatt_t_now_excerpt.csv: MeteoSwiss OGD file excerpt (quoted in the swissgeo.core documentation)
- smn/ogd-smn_meta_parameters.csv: lines of the official MeteoSwiss parameter list
- smn/ogd-smn_meta_stations.csv: lines (selected columns) of the official MeteoSwiss station list
Usage: python tests/fixtures/make_fixtures.py
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

D = Path(__file__).parent
UTC, ZRH = timezone.utc, ZoneInfo("Europe/Zurich")


def rng(start, end, step_min=10):
    t = start
    while t <= end:
        yield t
        t += timedelta(minutes=step_min)


def temp(t, base=10.0):
    return round(base + 4 * math.sin((t.hour + t.minute / 60 - 9) / 24 * 2 * math.pi), 1)


def write(path, text, encoding="utf-8"):
    (D / path).write_bytes(text.encode(encoding))


# ---------------------------------------------------------------- MeteoSwiss
SMN_COLS = ["tre200s0", "tre005s0", "ure200s0", "tde200s0", "prestas0", "rre150z0", "sre000z0", "gre000z0",
            "dkl010z0", "fu3010z0", "fkl010z0", "fu3010z1", "htoauts0"]


def smn_csv(code, start, end, cols=SMN_COLS):
    rows = [";".join(["station_abbr", "reference_timestamp"] + cols)]
    for i, t in enumerate(rng(start, end)):
        wind = 8 + 2 * math.cos(i / 9)
        v = {"tre200s0": temp(t), "tre005s0": temp(t) - 3, "ure200s0": 80.0, "tde200s0": round(temp(t) - 4, 1),
             "prestas0": 965.2, "rre150z0": 0.2 if i % 12 == 0 else 0.0, "sre000z0": 10 if 7 <= t.hour <= 16 else 0,
             "gre000z0": max(0, int(400 * math.sin((t.hour - 6) / 12 * math.pi))), "dkl010z0": 350 if i % 2 else 10,
             "fu3010z0": round(wind, 1), "fkl010z0": round(wind / 3.6, 1), "fu3010z1": round(wind * 1.8, 1), "htoauts0": "",
             "ta1tows0": temp(t) - 1, "fu3towz0": round(wind, 1), "fk1towz0": round(wind / 3.6, 1), "dv1towz0": 220}
        if i % 37 == 5:
            v["fu3010z0"] = ""
        rows.append(";".join([code, t.strftime("%d.%m.%Y %H:%M")] + [str(v[c]) for c in cols]))
    return "\n".join(rows) + "\n"


base = "https://data.geo.admin.ch/ch.meteoschweiz"
stac = {"assets": {n: {"href": f"{base}.ogd-smn/sma/{n}"} for n in
                   ["ogd-smn_sma_t_historical_2020-2029.csv", "ogd-smn_sma_t_recent.csv", "ogd-smn_sma_t_now.csv",
                    "ogd-smn_sma_h_now.csv", "ogd-smn_sma_d_recent.csv"]}}
write("smn/stac_ogd-smn_sma.json", json.dumps(stac, indent=1))
write("smn/ogd-smn_sma_t_now.csv", smn_csv("SMA", datetime(2026, 10, 2, 12, tzinfo=UTC), datetime(2026, 10, 3, 6, tzinfo=UTC)), "cp1252")
write("smn/ogd-smn_sma_t_recent.csv", smn_csv("SMA", datetime(2026, 10, 2, 0, tzinfo=UTC), datetime(2026, 10, 2, 23, 50, tzinfo=UTC)), "cp1252")
write("smn/ogd-smn_sma_t_historical_2020-2029.csv", smn_csv("SMA", datetime(2025, 6, 1, 0, tzinfo=UTC), datetime(2025, 6, 1, 2, tzinfo=UTC)), "cp1252")
stac_ueb = {"assets": {"ogd-smn-tower_ueb_t_now.csv": {"href": f"{base}.ogd-smn-tower/ueb/ogd-smn-tower_ueb_t_now.csv"}}}
write("smn/stac_ogd-smn-tower_ueb.json", json.dumps(stac_ueb, indent=1))
write("smn/ogd-smn-tower_ueb_t_now.csv", smn_csv("UEB", datetime(2026, 10, 2, 12, tzinfo=UTC), datetime(2026, 10, 3, 6, tzinfo=UTC),
                                                 ["ta1tows0", "fu3towz0", "fk1towz0", "dv1towz0", "sre000z0", "gre000z0"]), "cp1252")
write("smn/real_zermatt_t_now_excerpt.csv",
      "station_abbr;reference_timestamp;tre200s0;tre005s0;tresurs0;xchills0\n"
      "ZER;09.07.2026 00:00;14.2;9.1;7.8;14.2\nZER;09.07.2026 00:10;13.7;8.8;7.3;13.7\n"
      "ZER;09.07.2026 00:20;13.8;8.7;7;13.8\nZER;09.07.2026 00:30;13.3;8.4;7;13.3\n"
      "ZER;09.07.2026 00:40;13.3;8.3;6.6;13.3\nZER;09.07.2026 00:50;13.4;8.9;7.5;13.4\n", "cp1252")

head = ("parameter_shortname;parameter_description_de;parameter_description_fr;parameter_description_it;"
        "parameter_description_en;parameter_group_de;parameter_group_fr;parameter_group_it;parameter_group_en;"
        "parameter_granularity;parameter_decimals;parameter_datatype;parameter_unit")
params = [
    ("dkl010z0", "Windrichtung; Zehnminutenmittel", "Wind direction; ten minutes mean", "Wind", "T", "°"),
    ("fkl010z0", "Windgeschwindigkeit skalar; Zehnminutenmittel in m/s", "Wind speed scalar; ten minutes mean in m/s", "Wind", "T", "m/s"),
    ("fu3010z0", "Windgeschwindigkeit; Zehnminutenmittel in km/h", "Wind speed; ten minutes mean in km/h", "Wind", "T", "km/h"),
    ("fu3010z1", "Böenspitze (Sekundenböe); Maximum in km/h", "Gust peak (one second); maximum in km/h", "Wind", "T", "km/h"),
    ("gre000z0", "Globalstrahlung; Zehnminutenmittel", "Global radiation; ten minutes mean", "Strahlung", "T", "W/m²"),
    ("htoauts0", "Schneehöhe (automatisch gemessen); Momentanwert", "Snow depth (automatic measurement); current value", "Schnee", "T", "cm"),
    ("prestas0", "Luftdruck auf Barometerhöhe (QFE); Momentanwert", "Atmospheric pressure at barometric altitude (QFE); current value", "Druck", "T", "hPa"),
    ("rre150h0", "Niederschlag; Stundensumme", "Precipitation; hourly total", "Niederschlag", "H", "mm"),
    ("rre150z0", "Niederschlag; Zehnminutensumme", "Precipitation; ten minutes total", "Niederschlag", "T", "mm"),
    ("sre000z0", "Sonnenscheindauer; Zehnminutensumme", "Sunshine duration; ten minutes total", "Sonne", "T", "min"),
    ("tde200s0", "Taupunkt 2 m über Boden; Momentanwert", "Dew point 2 m above ground; current value", "Feuchte", "T", "°C"),
    ("tre005s0", "Lufttemperatur 5 cm über Gras; Momentanwert", "Air temperature at 5 cm above grass; current value", "Temperatur", "T", "°C"),
    ("tre200s0", "Lufttemperatur 2 m über Boden; Momentanwert", "Air temperature 2 m above ground; current value", "Temperatur", "T", "°C"),
    ("ure200s0", "Relative Luftfeuchtigkeit 2 m über Boden; Momentanwert", "Relative air humidity 2 m above ground; current value", "Feuchte", "T", "%"),
]
lines = [head] + [f'{c};"{de}";;;"{en}";{g};;;;{gran};1;Float;{u}' for c, de, en, g, gran, u in params]
write("smn/ogd-smn_meta_parameters.csv", "\n".join(lines) + "\n", "cp1252")
tower = [head] + [f'{c};"{de}";;;"{en}";{g};;;;T;1;Float;{u}' for c, de, en, g, u in [
    ("ta1tows0", "Lufttemperatur Turm; Momentanwert", "Air temperature tower; current value", "Temperatur", "°C"),
    ("fu3towz0", "Windgeschwindigkeit Turm; Zehnminutenmittel in km/h", "Wind speed tower; ten minutes mean in km/h", "Wind", "km/h"),
    ("fk1towz0", "Windgeschwindigkeit Turm; Zehnminutenmittel in m/s", "Wind speed tower; ten minutes mean in m/s", "Wind", "m/s"),
    ("dv1towz0", "Windrichtung vektoriell Turm; Zehnminutenmittel", "Wind direction vectorial tower; ten minutes mean", "Wind", "°"),
    ("sre000z0", "Sonnenscheindauer; Zehnminutensumme", "Sunshine duration; ten minutes total", "Sonne", "min"),
    ("gre000z0", "Globalstrahlung; Zehnminutenmittel", "Global radiation; ten minutes mean", "Strahlung", "W/m²")]]
write("smn/ogd-smn-tower_meta_parameters.csv", "\n".join(tower) + "\n", "cp1252")

st_head = "station_abbr;station_name;station_canton;station_height_masl;station_coordinates_wgs84_lat;station_coordinates_wgs84_lon"
write("smn/ogd-smn_meta_stations.csv", "\n".join([st_head,
      "ABO;Adelboden;BE;1321.0;46.491703;7.560703", "HOE;Hörnli;ZH;1133.0;47.370864;8.941644",
      "KLO;Zürich / Kloten;ZH;426.0;47.479611;8.535961", "LAE;Lägern;ZH;845.0;47.481933;8.397222",
      "PFA;Pfäffikon, ZH;ZH;537.0;47.376817;8.754864", "REH;Zürich / Affoltern;ZH;444.0;47.427694;8.517953"]) + "\n", "cp1252")
write("smn/ogd-smn-tower_meta_stations.csv", "\n".join([st_head,
      "BAN;Bantiger;BE;942.0;46.977806;7.528667", "UEB;Uetliberg;ZH;854.0;47.351364;8.490225"]) + "\n", "cp1252")
write("smn/ogd-smn-precip_meta_stations.csv", "\n".join([st_head, "TST;Teststation Niederschlag;ZH;500.0;47.3;8.6"]) + "\n", "cp1252")

# ---------------------------------------------------------------- Water police (Tecdottir + open-data CSV)
items = []
for t in rng(datetime(2026, 10, 2, 0, tzinfo=UTC), datetime(2026, 10, 3, 6, tzinfo=UTC)):
    local = t.astimezone(ZRH)
    vals = {"timestamp_cet": {"value": local.strftime("%d.%m.%Y %H:%M:%S"), "unit": ""},
            "air_temperature": {"value": temp(t, 12.0), "unit": "°C"}, "water_temperature": {"value": 18.4, "unit": "°C"},
            "wind_gust_max_10min": {"value": 3.1, "unit": "m/s"}, "wind_speed_avg_10min": {"value": 1.5, "unit": "m/s"},
            "wind_force_avg_10min": {"value": 1, "unit": "bft"}, "wind_direction": {"value": 200, "unit": "°"},
            "windchill": {"value": temp(t, 12.0), "unit": "°C"}, "barometric_pressure_qfe": {"value": 968.0, "unit": "hPa"},
            "precipitation": {"value": 0.0, "unit": "mm"}, "dew_point": {"value": 8.0, "unit": "°C"},
            "global_radiation": {"value": 120, "unit": "W/m²"}, "humidity": {"value": 70, "unit": "%"},
            "water_level": {"value": 405.27, "unit": "m"}}
    items.append({"station": "mythenquai", "values": vals})
write("wapo/tecdottir_mythenquai.json", json.dumps({"ok": True, "total_count": len(items), "result": items}))
ogd = ["_id,timestamp_utc,timestamp_cet,air_temperature,water_temperature,wind_gust_max_10min,wind_speed_avg_10min,"
       "wind_force_avg_10min,wind_direction,windchill,barometric_pressure_qfe,precipitation,dew_point,global_radiation,humidity,water_level"]
for i, t in enumerate(rng(datetime(2026, 8, 1, 0, tzinfo=UTC), datetime(2026, 8, 1, 1, tzinfo=UTC))):
    ogd.append(f"{i},{t.strftime('%Y-%m-%dT%H:%M:%S+00:00')},{t.astimezone(ZRH).strftime('%Y-%m-%dT%H:%M:%S%z')},"
               f"{temp(t, 20)},22.5,4.0,2.0,2,180,{temp(t, 20)},969.1,0.0,12.0,300,60,405.3")
write("wapo/messwerte_mythenquai_seit2007-heute.csv", "\n".join(ogd) + "\n")

# ---------------------------------------------------------------- BAFU
rows = []
for i, t in enumerate(rng(datetime(2026, 10, 1, 0, tzinfo=UTC), datetime(2026, 10, 3, 5, 50, tzinfo=UTC))):
    ts = t.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows += [{"timestamp": ts, "value": round(16 + 0.3 * math.sin(i / 20), 2), "parameterName": "WT", "unitSymbol": "°C", "station": {"no": "2099"}},
             {"timestamp": ts, "value": 30.5, "parameterName": "Q", "unitSymbol": "m³/s", "station": {"no": "2099"}},
             {"timestamp": ts, "value": 401.12, "parameterName": "W", "unitSymbol": "m ü.M.", "station": {"no": "2099"}}]
write("bafu/data_10min_mean.json", json.dumps(rows))
write("bafu/stations.json", json.dumps([{"no": "2099", "coverageFrom": "2001-01-01T00:00:00Z"}]))
write("bafu/station_list.json", json.dumps([
    {"no": "2099", "name": "Zürich, Unterhard", "riverName": "Limmat", "latitude": 47.39, "longitude": 8.53},
    {"no": "2176", "name": "Zürich, Sihlhölzli", "riverName": "Sihl", "latitude": 47.37, "longitude": 8.53},
    {"no": "2018", "name": "Brugg", "riverName": "Aare", "latitude": 47.48, "longitude": 8.19},      # outside bbox
    {"no": "2500", "name": "Grenzfall", "riverName": "Reuss", "latitude": 47.20, "longitude": 8.40},  # bbox, but canton AG
    {"no": "2999", "name": "ohne Koordinaten", "riverName": "X", "latitude": None, "longitude": None}]))

# ---------------------------------------------------------------- UGZ
ugz = ["Datum,Standort,Parameter,Intervall,Einheit,Wert,Status"]
for t in rng(datetime(2026, 10, 1, 22, tzinfo=UTC), datetime(2026, 10, 3, 5, tzinfo=UTC), 60):
    d = t.astimezone(ZRH).strftime("%Y-%m-%dT%H:%M%z")
    for st in ("Zch_Stampfenbachstrasse", "Zch_Schimmelstrasse"):
        for p, u, v in [("T", "°C", temp(t, 11)), ("Hr", "%Hr", 75), ("p", "hPa", 965.1), ("RainDur", "min", 0),
                        ("StrGlo", "W/m2", 100), ("WD", "°", 270), ("WVs", "m/s", 2.5), ("WVv", "m/s", 1.9)]:
            ugz.append(f"{d},{st},{p},h1,{u},{v},provisorisch")
write("ugz/ugz_ogd_meteo_h1_2026.csv", "\n".join(ugz) + "\n")

# ---------------------------------------------------------------- METAR
metar = []
for i, t in enumerate(rng(datetime(2026, 10, 2, 5, 50, tzinfo=UTC), datetime(2026, 10, 3, 5, 50, tzinfo=UTC), 30)):
    metar.append({"icaoId": "LSZH", "obsTime": int(t.timestamp()), "temp": round(temp(t)), "dewp": 5, "wspd": 6,
                  "wdir": "VRB" if i % 10 == 0 else 250, "wgst": None, "visib": "6+", "altim": 1018, "slp": None,
                  "clouds": [{"cover": "FEW", "base": 3000}], "rawOb": "METAR LSZH ..."})
    metar.append({"icaoId": "LSZB", "obsTime": int(t.timestamp()), "temp": 9, "wspd": 3})
write("metar/metar.json", json.dumps(metar))
write("metar/stationinfo.json", json.dumps([
    {"icaoId": "LSZH", "site": "Zurich", "lat": 47.458, "lon": 8.548, "country": "CH", "siteType": ["METAR", "TAF"]},
    {"icaoId": "LSMD", "site": "Dubendorf", "lat": 47.398, "lon": 8.648, "country": "CH", "siteType": ["METAR"]},
    {"icaoId": "LSZB", "site": "Bern", "lat": 46.912, "lon": 7.499, "country": "CH", "siteType": ["METAR", "TAF"]},
    {"icaoId": "LSXX", "site": "TAF only", "lat": 47.40, "lon": 8.60, "country": "CH", "siteType": ["TAF"]},
    {"icaoId": "EDNY", "site": "Friedrichshafen", "lat": 47.671, "lon": 9.511, "country": "DE", "siteType": ["METAR"]}]))
print("Fixtures written.")
