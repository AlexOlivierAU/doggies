from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


DEFAULT_HEADERS = {
    # Some public sites block non-browser-ish user agents.
    # This is a generic modern desktop UA string (no automation tools, no Selenium).
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class CachedResponse:
    url: str
    status_code: int
    text: str
    fetched_at_epoch: float
    from_cache: bool


_LAST_REQUEST_AT: float = 0.0
_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        _SESSION = s
    return _SESSION


def _rate_limit(min_interval_seconds: float = 1.0) -> None:
    global _LAST_REQUEST_AT
    now = time.time()
    elapsed = now - _LAST_REQUEST_AT
    if elapsed < min_interval_seconds:
        time.sleep(min_interval_seconds - elapsed)
    _LAST_REQUEST_AT = time.time()


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _cache_paths(cache_dir: Path, url: str) -> tuple[Path, Path]:
    key = _sha1(url)
    body_path = cache_dir / key
    meta_path = cache_dir / f"{key}.json"
    return body_path, meta_path


def get(
    url: str,
    *,
    cache_dir: str | os.PathLike = "./cache",
    timeout_seconds: float = 20.0,
    ttl_seconds: Optional[int] = 120,
    headers: Optional[dict[str, str]] = None,
) -> CachedResponse:
    """
    Fetch a URL with:
    - disk cache at ./cache/ using sha1(url) as filename
    - optional TTL (default 120s; set to None for "never expire")
    - 1 request/second rate limiting (only on cache miss/expired)
    """
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    body_path, meta_path = _cache_paths(cache_path, url)

    def load_cache() -> Optional[CachedResponse]:
        if not body_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = float(meta.get("fetched_at_epoch", 0))
            status_code = int(meta.get("status_code", 200))
            if ttl_seconds is not None and (time.time() - fetched_at) > ttl_seconds:
                return None
            text = body_path.read_text(encoding="utf-8", errors="replace")
            return CachedResponse(
                url=url,
                status_code=status_code,
                text=text,
                fetched_at_epoch=fetched_at,
                from_cache=True,
            )
        except Exception:
            return None

    cached = load_cache()
    if cached is not None:
        return cached

    _rate_limit(1.0)
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)

    try:
        s = _session()
        # per-request overrides
        resp = s.get(url, headers=h, timeout=timeout_seconds, allow_redirects=True)
    except requests.RequestException as e:
        raise FetchError(f"Network error while fetching {url}: {e}") from e

    # If blocked, try a one-time warmup request to homepage to get cookies, then retry.
    if resp.status_code in (401, 403):
        try:
            base = f"{resp.url.split('/')[0]}//{resp.url.split('/')[2]}/"
        except Exception:
            base = "https://www.thedogs.com.au/"
        try:
            _rate_limit(1.0)
            s.get(base, headers=h, timeout=timeout_seconds, allow_redirects=True)
            _rate_limit(1.0)
            resp = s.get(url, headers=h, timeout=timeout_seconds, allow_redirects=True)
        except requests.RequestException:
            pass

    text = resp.text or ""

    if resp.status_code >= 400:
        raise FetchError(f"HTTP {resp.status_code} while fetching {url}")

    # Only cache successful responses (avoid caching block pages / errors).
    try:
        body_path.write_text(text, encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {"url": url, "status_code": resp.status_code, "fetched_at_epoch": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        # Best-effort cache. If it fails, we still return the response.
        pass

    return CachedResponse(
        url=url,
        status_code=resp.status_code,
        text=text,
        fetched_at_epoch=time.time(),
        from_cache=False,
    )

