"""`HistoryViewCache`: a view is shared by concurrent readers and kept while the checkpoint id holds."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from base.agents.history.checkpoint import single_segment_history
from services.derived.insights.run_timeline import history
from services.derived.insights.run_timeline.history import HistoryViewCache


class FakeStore:
    """Counts checkpoint reads; `head` is the newest checkpoint id the store reports."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0) -> None:
        self.head: str | None = "c1"
        self.loads = 0
        self.probes = 0
        self.delay = delay
        monkeypatch.setattr(history, "load_checkpoint_history_full", self._load)
        monkeypatch.setattr(history, "latest_checkpoint_id", self._probe)

    def _load(self, db: Any, agent_id: int):
        self.loads += 1
        time.sleep(self.delay)
        return single_segment_history([HumanMessage(content="hi")])

    def _probe(self, db: Any, agent_id: int) -> str | None:
        self.probes += 1
        return self.head


DB: Any = SimpleNamespace()


def test_concurrent_cold_readers_share_one_build(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(monkeypatch, delay=0.2)
    cache = HistoryViewCache()
    views: list[Any] = []
    threads = [threading.Thread(target=lambda: views.append(cache.get(DB, 9))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.loads == 1
    assert all(v is views[0] for v in views)


def test_an_unchanged_checkpoint_keeps_the_view_past_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore(monkeypatch)
    monkeypatch.setattr(history, "_TTL_SECONDS", 0.0)
    cache = HistoryViewCache()
    first = cache.get(DB, 9)
    assert cache.get(DB, 9) is first
    assert (store.loads, store.probes) == (1, 2)


def test_a_new_checkpoint_rebuilds_the_view(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(monkeypatch)
    monkeypatch.setattr(history, "_TTL_SECONDS", 0.0)
    cache = HistoryViewCache()
    first = cache.get(DB, 9)
    store.head = "c2"
    assert cache.get(DB, 9) is not first
    assert store.loads == 2


def test_a_view_is_rebuilt_past_the_max_age(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(monkeypatch)
    monkeypatch.setattr(history, "_TTL_SECONDS", 0.0)
    monkeypatch.setattr(history, "_MAX_AGE_SECONDS", -1.0)
    cache = HistoryViewCache()
    cache.get(DB, 9)
    cache.get(DB, 9)
    assert store.loads == 2


def test_within_the_ttl_the_store_is_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(monkeypatch)
    cache = HistoryViewCache()
    cache.get(DB, 9)
    cache.get(DB, 9)
    assert (store.loads, store.probes) == (1, 1)


def test_one_page_of_agents_stays_cached_and_the_oldest_is_evicted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore(monkeypatch)
    cache = HistoryViewCache()
    cap = history._MAX_ENTRIES
    for agent_id in range(cap):
        cache.get(DB, agent_id)
    for agent_id in range(cap):
        cache.get(DB, agent_id)
    assert store.loads == cap
    cache.get(DB, cap)
    cache.get(DB, 0)
    assert store.loads == cap + 2
