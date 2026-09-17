"""Shared HTTP plumbing for the metadata sources: one rate limiter per API
(shared by the background worker and on-demand page requests, which run on
different threads) and polite retries on 429/503."""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

import requests


class MetadataError(RuntimeError):
    pass


class RateLimiter:
    """At most one request per `interval` seconds, across all threads."""

    def __init__(self, interval: float):
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class HttpClient:
    def __init__(self, user_agent: str, limiter: RateLimiter, headers: dict | None = None,
                 timeout: float = 15.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.session.headers["Accept"] = "application/json"
        if headers:
            self.session.headers.update(headers)
        self.limiter = limiter
        self.timeout = timeout

    def get_json(self, url: str, params: dict | None = None, tries: int = 3) -> dict | None:
        """GET -> parsed JSON, None on 404, MetadataError on anything else."""
        resp = self._request("GET", url, params, tries, allow_redirects=True)
        if resp.status_code == 404:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise MetadataError(f"GET {_host(url)}: invalid JSON") from exc

    def exists(self, url: str) -> bool:
        """Whether a URL answers with a redirect/200 rather than 404 - used for
        Cover Art Archive, whose image URLs 307 to archive.org when an image
        exists. Doesn't follow the redirect, so no image bytes are downloaded."""
        resp = self._request("HEAD", url, None, 2, allow_redirects=False)
        return resp.status_code in (200, 302, 303, 307, 308)

    def _request(self, method: str, url: str, params: dict | None, tries: int,
                 allow_redirects: bool) -> requests.Response:
        for attempt in range(tries):
            self.limiter.wait()
            try:
                resp = self.session.request(method, url, params=params, timeout=self.timeout,
                                            allow_redirects=allow_redirects)
            except requests.RequestException as exc:
                if attempt == tries - 1:
                    raise MetadataError(f"{method} {_host(url)}: {type(exc).__name__}") from exc
                continue
            if resp.status_code in (429, 503) and attempt < tries - 1:
                time.sleep(min(_retry_after(resp), 30))
                continue
            if resp.status_code >= 400 and resp.status_code != 404:
                raise MetadataError(f"{method} {_host(url)} -> {resp.status_code}")
            return resp
        raise MetadataError(f"{method} {_host(url)}: gave up after {tries} attempts")


def _retry_after(resp: requests.Response) -> float:
    try:
        return float(resp.headers.get("Retry-After", "2")) + 1
    except ValueError:
        return 3.0


def _host(url: str) -> str:
    # Error texts end up in the cached JSON; never let query strings (which
    # could one day carry a credential) leak into them.
    return urlsplit(url).netloc


def safe_url(value) -> str:
    """Only http(s) URLs may reach the browser as links or image sources."""
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    return ""
