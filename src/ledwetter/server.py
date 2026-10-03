"""Local web server: serves the visualisation UI and a small JSON API.

  GET /                 user interface (web/index.html)
  GET /api/series       all time series with station, parameter and time range
  GET /api/data         ?series=1,2&start=<unix>&end=<unix>&agg=raw|hour|day&min_coverage=0.75

Standard library only; intended for local use (binds to 127.0.0.1 by default).
"""
from __future__ import annotations

import json
import logging
import math
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .storage import AGG_SECONDS, MeasurementStore

log = logging.getLogger(__name__)
MAX_RAW_POINTS = 300_000


class ApiError(Exception):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, store: MeasurementStore, max_raw_points: int = MAX_RAW_POINTS):
        self.store, self.max_raw_points = store, max_raw_points

    def series(self) -> dict:
        return {"series": self.store.series()}

    def data(self, query: dict) -> dict:
        def one(name, default=None):
            v = query.get(name, [default])[0]
            if v is None:
                raise ApiError(f"Parameter '{name}' fehlt.")
            return v

        agg = one("agg", "raw")
        if agg not in ("raw", *AGG_SECONDS):
            raise ApiError("agg muss raw, hour oder day sein.")
        try:
            ids = [int(x) for x in one("series").split(",") if x]
            start, end = int(float(one("start"))), int(float(one("end")))
            min_cov = float(one("min_coverage", "0.75"))
        except ValueError:
            raise ApiError("series, start, end und min_coverage müssen Zahlen sein.")
        if start >= end:
            raise ApiError("start muss vor end liegen.")
        known = {s["id"] for s in self.store.series()}
        if unknown := [i for i in ids if i not in known]:
            raise ApiError(f"Unbekannte Reihen: {unknown}", HTTPStatus.NOT_FOUND)
        if agg == "raw":
            total = sum(self.store.count(i, start, end) for i in ids)
            if total > self.max_raw_points:
                raise ApiError(f"Zu viele Rohwerte ({total:,}). Wähle Stunden- oder Tageswerte "
                               f"oder einen kürzeren Zeitraum.".replace(",", "'"))
            out = [{"id": i, **self.store.raw(i, start, end)} for i in ids]
        else:
            out = [{"id": i, **self.store.aggregate(i, start, end, agg, min_cov)} for i in ids]
        return {"agg": agg, "start": start, "end": end, "series": out}


def _json_safe(obj):
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


class Handler(BaseHTTPRequestHandler):
    server_version = "ledwetter"

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        api: Api = self.server.api  # type: ignore[attr-defined]
        try:
            if url.path in ("/", "/index.html"):
                body = resources.files("ledwetter").joinpath("web/index.html").read_bytes()
                return self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            if url.path == "/api/series":
                return self._json(HTTPStatus.OK, api.series())
            if url.path == "/api/data":
                return self._json(HTTPStatus.OK, api.data(parse_qs(url.query)))
            raise ApiError("Nicht gefunden.", HTTPStatus.NOT_FOUND)
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:  # pragma: no cover - safety net
            log.exception("Fehler bei %s", self.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Interner Fehler: {e}"})

    def _json(self, status, payload):
        self._send(status, json.dumps(_json_safe(payload)).encode(), "application/json")

    def _send(self, status, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


def make_server(store: MeasurementStore, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.api = Api(store)  # type: ignore[attr-defined]
    return httpd
