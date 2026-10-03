"""Generates the synthetic test fixtures (run once; the output is committed).

The formats follow the documentation of each source. Exception: smn/real_zermatt_t_now_excerpt.csv
is a real file excerpt (MeteoSwiss OGD, quoted in the swissgeo.core documentation).
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


# ---------------------------------------------------------------- MeteoSchweiz
def smn_csv(code, start, end, temp_col="tre200s0", wind_col="fu3010z0", wind_scale=1.0, extra=True):
    head = ["station_abbr", "reference_timestamp", temp_col, wind_col] + (["tre005s0"] if extra else [])
    rows = [";".join(head)]
    for i, t in enumerate(rng(start, end)):
        w = "" if i % 37 == 5 else f"{round((8 + 2 * math.cos(i / 9)) * wind_scale, 1)}"
        row = [code, t.strftime("%d.%m.%Y %H:%M"), f"{temp(t)}", w] + ([f"{temp(t) - 3}"] if extra else [])
        rows.append(";".join(row))
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
                                                 temp_col="ta1tows0", wind_col="fk1towz0", wind_scale=1 / 3.6, extra=False), "cp1252")
write("smn/real_zermatt_t_now_excerpt.csv",
      "station_abbr;reference_timestamp;tre200s0;tre005s0;tresurs0;xchills0\n"
      "ZER;09.07.2026 00:00;14.2;9.1;7.8;14.2\nZER;09.07.2026 00:10;13.7;8.8;7.3;13.7\n"
      "ZER;09.07.2026 00:20;13.8;8.7;7;13.8\nZER;09.07.2026 00:30;13.3;8.4;7;13.3\n"
      "ZER;09.07.2026 00:40;13.3;8.3;6.6;13.3\nZER;09.07.2026 00:50;13.4;8.9;7.5;13.4\n", "cp1252")

# ---------------------------------------------------------------- WaPo (Tecdottir + OGD)
items = []
for t in rng(datetime(2026, 10, 2, 0, tzinfo=UTC), datetime(2026, 10, 3, 6, tzinfo=UTC)):
    local = t.astimezone(ZRH)
    items.append({"station": "mythenquai", "values": {
        "timestamp_cet": {"value": local.strftime("%d.%m.%Y %H:%M:%S"), "unit": ""},
        "air_temperature": {"value": temp(t, 12.0), "unit": "°C"},
        "water_temperature": {"value": 18.4, "unit": "°C"},
        "wind_speed_avg_10min": {"value": 1.5, "unit": "m/s"}}})
write("wapo/tecdottir_mythenquai.json", json.dumps({"ok": True, "total_count": len(items), "result": items}))
ogd = ["timestamp_utc,timestamp_cet,air_temperature,water_temperature,wind_gust_max_10min,wind_speed_avg_10min,humidity"]
for t in rng(datetime(2026, 8, 1, 0, tzinfo=UTC), datetime(2026, 8, 1, 1, tzinfo=UTC)):
    ogd.append(f"{t.strftime('%Y-%m-%dT%H:%M:%S+00:00')},{t.astimezone(ZRH).strftime('%Y-%m-%dT%H:%M:%S%z')},"
               f"{temp(t, 20)},22.5,4.0,2.0,60")
write("wapo/messwerte_mythenquai_seit2007-heute.csv", "\n".join(ogd) + "\n")

# ---------------------------------------------------------------- BAFU
rows = [{"timestamp": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": round(16 + 0.3 * math.sin(i / 20), 2),
         "station": {"no": "2099"}} for i, t in enumerate(rng(datetime(2026, 10, 1, 0, tzinfo=UTC), datetime(2026, 10, 3, 5, 50, tzinfo=UTC)))]
write("bafu/data_10min_mean.json", json.dumps(rows))
write("bafu/stations.json", json.dumps([{"no": "2099", "coverageFrom": "2001-01-01T00:00:00Z"}]))

# ---------------------------------------------------------------- UGZ
ugz = ["Datum,Standort,Parameter,Intervall,Einheit,Wert,Status"]
for t in rng(datetime(2026, 10, 1, 22, tzinfo=UTC), datetime(2026, 10, 3, 5, tzinfo=UTC), 60):
    d = t.astimezone(ZRH).strftime("%Y-%m-%dT%H:%M%z")
    for st in ("Zch_Stampfenbachstrasse", "Zch_Schimmelstrasse"):
        ugz += [f"{d},{st},T,h1,°C,{temp(t, 11)},provisorisch", f"{d},{st},WVs,h1,m/s,2.5,provisorisch",
                f"{d},{st},WVv,h1,m/s,1.9,provisorisch", f"{d},{st},p,h1,hPa,965.1,provisorisch"]
write("ugz/ugz_ogd_meteo_h1_2026.csv", "\n".join(ugz) + "\n")

# ---------------------------------------------------------------- METAR
metar = []
for t in rng(datetime(2026, 10, 2, 5, 50, tzinfo=UTC), datetime(2026, 10, 3, 5, 50, tzinfo=UTC), 30):
    metar.append({"icaoId": "LSZH", "obsTime": int(t.timestamp()), "temp": round(temp(t)), "wspd": 6})
    metar.append({"icaoId": "LSZB", "obsTime": int(t.timestamp()), "temp": 9, "wspd": 3})
write("metar/metar.json", json.dumps(metar))
print("Fixtures written.")
