"""Bounded TTL + single-flight cache for the expensive inspector aggregate."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass


class InspectCacheFullError(RuntimeError):
    """Too many distinct inspector keys are already in flight."""


@dataclass(frozen=True)
class _CacheHit[V]:
    value: V


@dataclass(frozen=True)
class _CacheClaim[V]:
    future: Future[V]
    leader: bool


class InspectQueryCache[K, V]:
    """Share one loader per key and retain successful values for a short TTL."""

    def __init__(self, *, max_entries: int, max_inflight: int) -> None:
        self._max_entries = max_entries
        self._max_inflight = max_inflight
        self._values: dict[K, tuple[float, V]] = {}
        self._inflight: dict[K, Future[V]] = {}
        self._lock = threading.Lock()

    def get_or_load(
        self,
        key: K,
        loader: Callable[[], V],
        *,
        ttl_s: float,
        now: Callable[[], float],
    ) -> V:
        claim = self._lookup_or_claim(key, now())
        if isinstance(claim, _CacheHit):
            return claim.value
        if not claim.leader:
            return claim.future.result()
        return self._load_claim(key, claim.future, loader, ttl_s=ttl_s, now=now)

    def _lookup_or_claim(self, key: K, current: float) -> _CacheHit[V] | _CacheClaim[V]:
        with self._lock:
            hit = self._values.get(key)
            if hit is not None:
                if hit[0] > current:
                    return _CacheHit(hit[1])
                del self._values[key]
            future: Future[V] | None = self._inflight.get(key)
            if future is not None:
                return _CacheClaim(future, leader=False)
            if len(self._inflight) >= self._max_inflight:
                raise InspectCacheFullError
            future = Future[V]()
            self._inflight[key] = future
            return _CacheClaim(future, leader=True)

    def _load_claim(
        self,
        key: K,
        future: Future[V],
        loader: Callable[[], V],
        *,
        ttl_s: float,
        now: Callable[[], float],
    ) -> V:
        try:
            value = loader()
            current = now()
            self._store(key, value, current=current, expires_at=current + ttl_s)
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(key) is future:
                    del self._inflight[key]

    def _store(self, key: K, value: V, *, current: float, expires_at: float) -> None:
        with self._lock:
            if len(self._values) >= self._max_entries:
                expired = [
                    item_key for item_key, item in self._values.items() if item[0] <= current
                ]
                for expired_key in expired:
                    del self._values[expired_key]
                if len(self._values) >= self._max_entries:
                    oldest = min(self._values, key=lambda item_key: self._values[item_key][0])
                    del self._values[oldest]
            self._values[key] = (expires_at, value)

    def clear(self) -> None:
        """Drop state between isolated tests; callers ensure no load is active."""
        with self._lock:
            self._values.clear()
            self._inflight.clear()
