"""Shared test infrastructure: a fixed clock and an HTTP test double that serves fixtures."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests
import yaml

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, content: bytes = b"", status: int = 200):
        self.content, self.status_code = content, status

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeHttp:
    """Replaces HttpClient. routes: (regex, handler(match, params) -> FakeResponse | bytes | dict | list)."""

    def __init__(self, routes=(), post_handler=None):
        self.routes = list(routes)
        self.post_handler = post_handler
        self.calls: list = []

    def get(self, url, **params):
        self.calls.append(("GET", url, params))
        for pattern, handler in self.routes:
            m = re.search(pattern, url)
            if m:
                res = handler(m, params)
                if isinstance(res, FakeResponse):
                    res.raise_for_status()
                    return res
                if isinstance(res, (dict, list)):
                    return FakeResponse(json.dumps(res).encode())
                return FakeResponse(res)
        raise requests.HTTPError(f"404 (no route) {url}")

    def post_json(self, url, payload):
        self.calls.append(("POST", url, payload))
        if self.post_handler is None:
            raise requests.HTTPError("404")
        return self.post_handler(payload)


def fixture_bytes(rel: str) -> bytes:
    path = FIXTURES / rel
    if not path.exists():
        raise requests.HTTPError(f"404 {rel}")
    return path.read_bytes()


def file_route(template):
    def handler(m, params):
        try:
            return fixture_bytes(template.format(*m.groups()))
        except requests.HTTPError:
            return FakeResponse(b"", 404)
    return handler


def bafu_handler(payload):
    """Answers GraphQL queries from the fixtures and honours the requested time window."""
    q = payload["query"]
    if "stations(" in q:
        return {"data": {"water": {"observations": {"stations": json.loads(fixture_bytes("bafu/stations.json"))}}}}
    if "data_10min_mean" in q:
        gte = re.search(r'_gte: "([^"]+)"', q).group(1)
        lt = re.search(r'_lt: "([^"]+)"', q).group(1)
        ids = json.loads(re.search(r"_in: (\[[^\]]*\])", q).group(1))
        rows = [r for r in json.loads(fixture_bytes("bafu/data_10min_mean.json"))
                if gte <= r["timestamp"] < lt and r["station"]["no"] in ids]
        return {"data": {"water": {"observations": {"data_10min_mean": rows}}}}
    if "data_live" in q:
        return {"data": {"water": {"observations": {"data_live": [
            {"stationNo": "2099", "timestamp": "2026-10-03T05:47:00Z", "value": 16.1}]}}}}
    return {"errors": [{"message": "unknown query"}]}


def make_fake_http() -> FakeHttp:
    def wapo_api(m, params):
        if params.get("offset", 0):
            return {"ok": True, "result": []}
        return fixture_bytes(f"wapo/tecdottir_{m.group(1)}.json")

    routes = [
        (r"/api/stac/v1/collections/ch\.meteoschweiz\.(ogd-smn(?:-tower)?)/items/(\w+)$", file_route("smn/stac_{}_{}.json")),
        (r"/ch\.meteoschweiz\.[\w-]+/\w+/([\w.-]+\.csv)$", file_route("smn/{}")),
        (r"tecdottir.*/measurements/(\w+)$", wapo_api),
        (r"sid_wapo_wetterstationen/download/([\w.-]+\.csv)$", file_route("wapo/{}")),
        (r"(ugz_ogd_meteo_h1_\d{4}\.csv)$", file_route("ugz/{}")),
        (r"aviationweather\.gov/api/data/metar", lambda m, p: fixture_bytes("metar/metar.json")),
    ]
    return FakeHttp(routes, bafu_handler)


@pytest.fixture
def clock():
    return lambda: FIXED_NOW


@pytest.fixture
def fake_http():
    return make_fake_http()


@pytest.fixture
def test_config():
    return yaml.safe_load((FIXTURES / "stations_test.yaml").read_text(encoding="utf-8"))
