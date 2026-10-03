"""HTTP client with retries and exponential backoff."""
from __future__ import annotations

import time

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
