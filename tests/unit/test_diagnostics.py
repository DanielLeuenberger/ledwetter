"""Recording, replay and the diagnose command, using a stub in place of the network."""
import json
import zipfile
from datetime import timedelta

import pandas as pd
import pytest
import requests

from conftest import FIXED_NOW, make_fake_http
from ledwetter import diagnose
from ledwetter.http import RecordingHttpClient, ReplayHttpClient
from ledwetter.ingest import Ingestor
from ledwetter.sources import build_sources
from ledwetter.storage import MeasurementStore

SINCE = FIXED_NOW - timedelta(hours=6)


def _response(url, call):
    r = requests.models.Response()
    r.url = url
    try:
        content = call()
        r.status_code, r._content = 200, content
        r.headers["Content-Type"] = "application/json" if content[:1] in (b"{", b"[") else "text/csv"
    except requests.HTTPError:
        r.status_code, r._content = 404, b""
    return r


class StubSession:
    """Stands in for requests.Session and answers from the test fixtures."""

    def __init__(self):
        self.fake, self.headers = make_fake_http(), {}

    def mount(self, *args):
        pass

    def get(self, url, params=None, timeout=None):
        return _response(url, lambda: self.fake.get(url, **(params or {})).content)

    def post(self, url, json=None, timeout=None):
        import json as js
        return _response(url, lambda: js.dumps(self.fake.post_json(url, json)).encode())


def recording_client(path, **kw):
    client = RecordingHttpClient(path, **kw)
    client.session = StubSession()
    return client


def test_recording_writes_index_and_files(tmp_path):
    client = recording_client(tmp_path / "rec")
    client.get("https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/ogd-smn_meta_parameters.csv")
    with pytest.raises(requests.HTTPError):
        client.get("https://data.geo.admin.ch/gibt/es/nicht.csv")
    entries = [json.loads(l) for l in (tmp_path / "rec" / "index.jsonl").read_text().splitlines()]
    assert [e["status"] for e in entries] == [200, 404]
    assert (tmp_path / "rec" / entries[0]["file"]).read_bytes().startswith(b"parameter_shortname")


def test_large_text_is_truncated_at_line_boundary_json_is_not(tmp_path):
    client = recording_client(tmp_path / "rec", max_bytes=300)
    client.get("https://data.geo.admin.ch/ch.meteoschweiz.ogd-smn/sma/ogd-smn_sma_t_now.csv")
    client.post_json("https://data.bafu.admin.ch/api", {"query": "{ water { observations { stations(where: {no: {_in: [\"2099\"]}}) { no coverageFrom } } } }"})
    csv_entry, json_entry = [json.loads(l) for l in (tmp_path / "rec" / "index.jsonl").read_text().splitlines()]
    data = (tmp_path / "rec" / csv_entry["file"]).read_bytes()
    assert csv_entry["truncated"] and len(data) <= 300 and data.endswith(b"\n")
    assert not json_entry["truncated"]
    json.loads((tmp_path / "rec" / json_entry["file"]).read_bytes())


def test_replay_answers_recorded_requests_and_404_otherwise(tmp_path):
    client = recording_client(tmp_path / "rec")
    url = "https://aviationweather.gov/api/data/metar"
    original = client.get(url, ids="LSZH", format="json", hours=3).content
    replay = ReplayHttpClient(tmp_path / "rec")
    assert replay.get(url, ids="LSZH", format="json", hours=3).content == original
    with pytest.raises(requests.HTTPError):
        replay.get(url, ids="LSZB", format="json", hours=3)
    assert replay.misses == [("GET", url, {"ids": "LSZB", "format": "json", "hours": 3})]


def test_diagnose_run_and_exact_replay(tmp_path, clock):
    out = tmp_path / "diagnose"
    archive = diagnose.run(recording_client(out), {}, str(out), "ZH", SINCE, clock)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
    assert {"diagnose/summary.csv", "diagnose/index.jsonl", "diagnose/diagnose.log", "diagnose/stations_zh.yaml"} <= set(names)
    recorded = pd.read_csv(out / "summary.csv")
    assert {"meteoschweiz", "wapo", "bafu", "ugz", "metar"} <= set(recorded["source"])
    assert (recorded["count"] > 0).all()

    # Replaying the recording without network gives exactly the same database content
    replay = ReplayHttpClient(out)
    import yaml
    cfg = yaml.safe_load((out / "stations_zh.yaml").read_text(encoding="utf-8"))
    store = MeasurementStore(tmp_path / "replay.db")
    Ingestor(store, build_sources(cfg, replay, clock=clock), clock).update(SINCE)
    replayed = diagnose.summarise(store)
    cols = ["source", "station_id", "parameter", "count", "min", "max"]
    pd.testing.assert_frame_equal(recorded[cols].sort_values(cols[:3]).reset_index(drop=True),
                                  replayed[cols].sort_values(cols[:3]).reset_index(drop=True), check_dtype=False)
