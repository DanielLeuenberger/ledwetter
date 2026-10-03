import json
import threading
import urllib.error
import urllib.request

import pandas as pd
import pytest

from ledwetter.model import make_frame
from ledwetter.server import make_server
from ledwetter.storage import MeasurementStore


@pytest.fixture
def server(tmp_path):
    store = MeasurementStore(tmp_path / "s.db")
    t = pd.date_range("2026-10-03T00:10Z", "2026-10-03T06:00Z", freq="10min")
    store.upsert(make_frame(t, list(range(len(t))), source="meteoschweiz", station_id="SMA", station_name="Fluntern",
                            parameter="tre200s0", unit="°C", time_ref="interval_end", interval_min=10))
    httpd = make_server(store, "127.0.0.1", 0)
    httpd.api.max_raw_points = 20
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    store.close()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_index_is_served(server):
    status, ctype, body = get(server + "/")
    assert status == 200 and ctype.startswith("text/html") and b"Diagramm zeichnen" in body


def test_series_endpoint(server):
    status, _, body = get(server + "/api/series")
    (s,) = json.loads(body)["series"]
    assert status == 200 and s["station_id"] == "SMA" and s["parameter"] == "tre200s0" and s["agg"] == "mean"


def test_hourly_data(server):
    start = int(pd.Timestamp("2026-10-03T00:00Z").timestamp())
    end = int(pd.Timestamp("2026-10-03T06:00Z").timestamp())
    status, _, body = get(f"{server}/api/data?series=1&start={start}&end={end}&agg=hour")
    data = json.loads(body)
    assert status == 200 and data["agg"] == "hour" and len(data["series"][0]["t"]) == 6 and "value" in data["series"][0]


def test_raw_limit_and_validation(server):
    s, _, body = get(f"{server}/api/data?series=1&start=0&end=1891000000&agg=raw")
    assert s == 400 and "Zu viele Rohwerte" in json.loads(body)["error"]
    assert get(f"{server}/api/data?series=1&start=10&end=5")[0] == 400
    assert get(f"{server}/api/data?series=99&start=0&end=5")[0] == 404
    assert get(f"{server}/api/data?series=1&start=0&end=5&agg=week")[0] == 400
    assert get(f"{server}/gibtsnicht")[0] == 404
