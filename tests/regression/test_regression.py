"""Regression tests: the complete pipeline on recorded source responses, compared with reference outputs.

Regenerate the references (only after an intentional change!):  UPDATE_GOLDEN=1 pytest tests/regression
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from conftest import FIXED_NOW, FIXTURES, make_fake_http
from ledwetter.export import collect, write_csv
from ledwetter.ingest import Ingestor
from ledwetter.model import Period
from ledwetter.sources import MeteoSwissSMN, build_sources
from ledwetter.storage import MeasurementStore

pytestmark = pytest.mark.regression
EXPECTED = Path(__file__).parent / "expected"
PERIOD = Period(FIXED_NOW - timedelta(hours=24), FIXED_NOW)
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def check_golden(name: str, actual: str) -> None:
    path = EXPECTED / name
    if UPDATE or not path.exists():
        path.write_text(actual, encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"Reference {name} was missing and has been created – review and commit it.")
        return
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        diff = [f"- {e}\n+ {a}" for e, a in zip(expected.splitlines(), actual.splitlines()) if e != a][:5]
        pytest.fail(f"{name} differs from the reference ({len(expected.splitlines())} vs. "
                    f"{len(actual.splitlines())} lines). First differences:\n" + "\n".join(diff))


def sources(test_config, clock):
    return build_sources(test_config, make_fake_http(), clock=clock)


def test_export_csv_matches_reference(tmp_path, test_config, clock):
    out = tmp_path / "export.csv"
    write_csv(collect(sources(test_config, clock), PERIOD), str(out))
    check_golden("export_24h.csv", out.read_text(encoding="utf-8"))


def test_export_invariants(tmp_path, test_config, clock):
    out = tmp_path / "export.csv"
    write_csv(collect(sources(test_config, clock), PERIOD), str(out))
    df = pd.read_csv(out)
    assert set(df["source"]) == {"meteoschweiz", "wapo", "bafu", "ugz", "metar"}
    assert not df.duplicated(["time_utc", "source", "station_id", "parameter", "time_ref"]).any()
    t = pd.to_datetime(df["time_utc"], utc=True)
    assert t.min() >= pd.Timestamp(PERIOD.start) and t.max() <= pd.Timestamp(PERIOD.end)
    assert set(df["unit"]) <= {"°C", "km/h"}
    assert set(df["time_ref"]) <= {"interval_end", "interval_start", "instant", "unknown"}


def test_database_aggregates_match_reference(tmp_path, test_config, clock):
    store = MeasurementStore(tmp_path / "r.db")
    Ingestor(store, sources(test_config, clock), clock).update(since=PERIOD.start)
    a, b = int(PERIOD.start.timestamp()), int(PERIOD.end.timestamp())
    result = {}
    for s in store.series():
        key = f"{s['source']}/{s['station_id']}/{s['parameter']}/{s['time_ref']}"
        result[key] = {agg: store.aggregate(s["id"], a, b, agg, 0.75) for agg in ("hour", "day")}
        result[key]["raw_count"] = store.count(s["id"], a, b)
    store.close()
    check_golden("aggregates_24h.json", json.dumps(result, indent=1, sort_keys=True) + "\n")


def test_database_is_idempotent(tmp_path, test_config, clock):
    store = MeasurementStore(tmp_path / "i.db")
    ing = Ingestor(store, sources(test_config, clock), clock)
    ing.update(since=PERIOD.start)
    before = store.conn.execute("SELECT COUNT(*), SUM(value) FROM measurements").fetchone()
    ing.update(since=PERIOD.start)
    assert store.conn.execute("SELECT COUNT(*), SUM(value) FROM measurements").fetchone() == before
    store.close()


def test_real_meteoswiss_excerpt_zermatt(clock):
    """Real excerpt of an SMN file (t_now, Zermatt) – guards the file format."""
    src = MeteoSwissSMN(make_fake_http(), {"stations": []}, clock)
    text = (FIXTURES / "smn/real_zermatt_t_now_excerpt.csv").read_bytes().decode("cp1252")
    df = src.parse(text, {"code": "ZER", "name": "Zermatt"}, src.cfg["params"]["ogd-smn"])
    assert df["time_utc"].dt.strftime("%Y-%m-%dT%H:%MZ").tolist() == [
        "2026-07-09T00:00Z", "2026-07-09T00:10Z", "2026-07-09T00:20Z",
        "2026-07-09T00:30Z", "2026-07-09T00:40Z", "2026-07-09T00:50Z"]
    assert df["value"].tolist() == [14.2, 13.7, 13.8, 13.3, 13.3, 13.4]
    assert set(df["parameter"]) == {"air_temperature"}
