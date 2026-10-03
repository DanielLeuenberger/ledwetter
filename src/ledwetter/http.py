"""HTTP clients: live client with retries, plus recording and replay for diagnostics.

RecordingHttpClient stores every response in a directory (one file per response plus index.jsonl).
ReplayHttpClient answers requests from such a directory without network access, so that a run on
one machine can be reproduced exactly elsewhere (e.g. to debug an import or to build test fixtures).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class HttpClient:
    def __init__(self, user_agent: str = "ledwetter/0.1", timeout: int = 60, delay: float = 0.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "POST"))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.timeout = timeout
        self.delay = delay  # pause before every request to spare the servers during backfills

    def get(self, url: str, **params) -> requests.Response:
        if self.delay:
            time.sleep(self.delay)
        res = self.session.get(url, params=params or None, timeout=self.timeout)
        res.raise_for_status()
        return res

    def post_json(self, url: str, payload: dict) -> dict:
        if self.delay:
            time.sleep(self.delay)
        res = self.session.post(url, json=payload, timeout=self.timeout)
        res.raise_for_status()
        return res.json()


def request_key(method: str, url: str, data) -> str:
    """Stable key of a request: method, URL and sorted parameters or payload."""
    return f"{method} {url} {json.dumps(data or {}, sort_keys=True, default=str)}"


_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?Z?")


def loose_key(method: str, url: str, data) -> str:
    """Request key with all ISO timestamps removed, used when the exact key is not in a recording."""
    return _TIME_RE.sub("<t>", request_key(method, url, data))


def _slug(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1] or "response"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]


class RecordingHttpClient(HttpClient):
    """Live client that additionally stores every response (and every HTTP error) in a directory.

    Text files larger than max_bytes (CSV etc.) are cut at a line boundary to keep the recording small;
    JSON responses are always stored completely so that they stay valid.
    """

    def __init__(self, directory: str | Path, *args, max_bytes: int = 2_000_000, **kwargs):
        super().__init__(*args, **kwargs)
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._n = len(list(self.dir.glob("[0-9]*_*")))

    def _store(self, method: str, url: str, data, status: int, content: bytes, ctype: str) -> None:
        is_json = "json" in ctype or content[:1] in (b"{", b"[")
        truncated = False
        if not is_json and len(content) > self.max_bytes:
            cut = content.rfind(b"\n", 0, self.max_bytes)
            content, truncated = content[: cut + 1 if cut > 0 else self.max_bytes], True
        with self._lock:
            self._n += 1
            ext = ".json" if is_json else ".csv" if url.endswith(".csv") else ".txt"
            fname = f"{self._n:04d}_{_slug(url)}" + ("" if _slug(url).endswith(ext) else ext)
            (self.dir / fname).write_bytes(content)
            entry = {"n": self._n, "method": method, "url": url, "data": data, "status": status,
                     "content_type": ctype, "bytes": len(content), "truncated": truncated, "file": fname,
                     "key": hashlib.sha1(request_key(method, url, data).encode()).hexdigest()}
            with open(self.dir / "index.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get(self, url: str, **params) -> requests.Response:
        try:
            res = super().get(url, **params)
        except requests.HTTPError as e:
            r = e.response
            self._store("GET", url, params, r.status_code if r is not None else 0,
                        r.content if r is not None else b"", r.headers.get("Content-Type", "") if r is not None else "")
            raise
        self._store("GET", url, params, res.status_code, res.content, res.headers.get("Content-Type", ""))
        return res

    def post_json(self, url: str, payload: dict) -> dict:
        if self.delay:
            time.sleep(self.delay)
        res = self.session.post(url, json=payload, timeout=self.timeout)
        self._store("POST", url, payload, res.status_code, res.content, res.headers.get("Content-Type", ""))
        res.raise_for_status()
        return res.json()


class ReplayResponse:
    def __init__(self, content: bytes, status: int, url: str):
        self.content, self.status_code, self.url = content, status, url
        self.text = content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} (replayed) {self.url}", response=self)


class ReplayHttpClient:
    """Answers requests from a recording directory. Unknown requests raise HTTPError 404."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.entries: dict = {}
        self.loose: dict = {}
        for line in (self.dir / "index.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                self.entries[e["key"]] = e   # the latest recording of a request wins
                self.loose.setdefault(loose_key(e["method"], e["url"], e["data"]), []).append(e)
        self.misses: list = []

    def _lookup(self, method: str, url: str, data) -> ReplayResponse:
        key = hashlib.sha1(request_key(method, url, data).encode()).hexdigest()
        e = self.entries.get(key)
        if e is None:  # e.g. a query whose time window depends on the clock: match without timestamps
            candidates = self.loose.get(loose_key(method, url, data), [])
            e = candidates.pop(0) if candidates else None
        if e is None:
            self.misses.append((method, url, data))
            return ReplayResponse(b"", 404, url)
        return ReplayResponse((self.dir / e["file"]).read_bytes(), e["status"], url)

    def get(self, url: str, **params) -> ReplayResponse:
        res = self._lookup("GET", url, params)
        res.raise_for_status()
        return res

    def post_json(self, url: str, payload: dict) -> dict:
        res = self._lookup("POST", url, payload)
        res.raise_for_status()
        return res.json()
