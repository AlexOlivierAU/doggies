"""Asynchronous jockey-silk loader. Never called from paint()."""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkDiskCache, QNetworkReply, QNetworkRequest

from desktop.paths import desktop_log_dir

log = logging.getLogger("race_day_rater.silks")

_MAX_MEMORY = 256


class SilkCache(QObject):
    silk_ready = Signal(str)

    def __init__(self, parent=None, *, network_enabled: bool = True) -> None:
        super().__init__(parent)
        self._memory: dict[str, QPixmap] = {}
        self._failed: set[str] = set()
        self._inflight: set[str] = set()
        self._network_enabled = network_enabled
        self._nam: QNetworkAccessManager | None = None
        self._closed = False
        self._get_count = 0
        if network_enabled:
            self._nam = QNetworkAccessManager(self)
            cache = QNetworkDiskCache(self)
            root = desktop_log_dir() / "silks"
            try:
                root.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            cache.setCacheDirectory(str(root))
            cache.setMaximumCacheSize(40 * 1024 * 1024)
            self._nam.setCache(cache)
            self._nam.finished.connect(self._on_finished)

    def close(self) -> None:
        self._closed = True
        self._inflight.clear()
        if self._nam is not None:
            try:
                self._nam.finished.disconnect(self._on_finished)
            except Exception:
                pass

    def pixmap(self, url: str) -> Optional[QPixmap]:
        if not url or url in self._failed:
            return None
        return self._memory.get(url)

    def is_failed(self, url: str) -> bool:
        return bool(url) and url in self._failed

    def is_inflight(self, url: str) -> bool:
        return url in self._inflight

    def inject(self, url: str, pixmap: QPixmap) -> None:
        """Tests / demo: put a pixmap in memory without downloading."""
        if not url:
            return
        self._failed.discard(url)
        self._memory[url] = pixmap
        self._trim()
        if not self._closed:
            self.silk_ready.emit(url)

    def mark_failed(self, url: str) -> None:
        if url:
            self._failed.add(url)
            self._inflight.discard(url)

    def request(self, url: str) -> None:
        if self._closed or not url or url in self._memory or url in self._failed or url in self._inflight:
            return
        if url.startswith("file:"):
            local = QUrl(url).toLocalFile()
            pm = QPixmap(local)
            if pm.isNull():
                self.mark_failed(url)
                return
            self.inject(url, pm)
            return
        if not self._network_enabled or self._nam is None:
            return
        self._inflight.add(url)
        self._get_count += 1
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute,
            QNetworkRequest.CacheLoadControl.PreferCache,
        )
        req.setMaximumRedirectsAllowed(4)
        self._nam.get(req)

    def prefetch(self, urls) -> None:
        seen: set[str] = set()
        for url in urls or []:
            u = str(url or "")
            if u and u not in seen:
                seen.add(u)
                self.request(u)

    def _on_finished(self, reply: QNetworkReply) -> None:
        url = reply.request().url().toString()
        self._inflight.discard(url)
        try:
            if self._closed:
                return
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self.mark_failed(url)
                return
            data = reply.readAll().data()
            pm = QPixmap()
            if not data or not pm.loadFromData(data):
                self.mark_failed(url)
                return
            self._memory[url] = pm
            self._trim()
            self.silk_ready.emit(url)
            try:
                self._write_file_cache(url, data)
            except Exception:
                pass
        finally:
            reply.deleteLater()

    def _write_file_cache(self, url: str, data: bytes) -> None:
        root = desktop_log_dir() / "silks" / "files"
        root.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24] + ".png"
        path = root / name
        if not path.exists():
            path.write_bytes(data)

    def _trim(self) -> None:
        extra = len(self._memory) - _MAX_MEMORY
        if extra <= 0:
            return
        for key in list(self._memory.keys())[:extra]:
            self._memory.pop(key, None)


_CACHE: SilkCache | None = None


def silk_cache(parent=None, *, network_enabled: bool = True) -> SilkCache:
    global _CACHE
    if _CACHE is None or _CACHE._closed:
        _CACHE = SilkCache(parent, network_enabled=network_enabled)
    return _CACHE


def reset_silk_cache() -> None:
    global _CACHE
    if _CACHE is not None:
        _CACHE.close()
    _CACHE = None
