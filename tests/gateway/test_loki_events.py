"""Unit tests for `gateway/loki_events.py` — the Loki read side of the
unified event stream (task #1197, LGTM cutover).

The module's only I/O is httpx GETs through the shared client accessor
(`loki_events._client`); these tests swap the accessor for a fake and assert
on the LogQL text, the query params, and the parse/paging semantics
(newest-first merge across streams, in-memory offset paging, +1 lookahead
`has_more`).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from gateway import _loki_logql, loki_events, loki_events_cache, loki_query_budget
from shared.events.contract import lineage_event_names
from shared.loki_index_labels import (
    EVENT_STREAM_RETENTION,
    LINEAGE_RETENTION_PERIOD,
    LOKI_MAX_QUERY_SERIES,
    LOKI_QUERY_CONCURRENCY,
    WAL_DISK_FULL_THRESHOLD,
    LokiReadEra,
    LokiReadSlice,
    event_stream_selector,
    validate_loki_deploy_config,
)


def _selector_event_names(selector: str) -> set[str]:
    """The event_name alternation inside a `retention_stream` selector."""
    match = re.fullmatch(r'\{event_name=~"([a-z0-9_|]+)"\}', selector)
    return set(match.group(1).split("|")) if match else set()


# ─── fake httpx transport ────────────────────────────────────────────────────


class _FakeResponse:
    """Minimal httpx.Response stand-in: JSON payload + raise_for_status."""

    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"loki {self.status_code}")


class _FakeClient:
    """Records the request, returns canned payloads; stands in for the shared
    client behind `loki_events._client()`."""

    def __init__(self, payloads: list[dict[str, Any] | _FakeResponse] | dict[str, Any]) -> None:
        self.payloads = payloads if isinstance(payloads, list) else [payloads]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
        self.calls.append((url, params))
        if not self.payloads:
            # attribute_aggregate slices a window into multiple instant
            # queries (era-sliced read path, task #1407 B2); a test that
            # canned a single payload legitimately sees later slices with no
            # data. Loki itself returns an empty result for an empty window,
            # so return that instead of raising.
            return _FakeResponse({"data": {"result": []}})
        item = self.payloads.pop(0)
        return item if isinstance(item, _FakeResponse) else _FakeResponse(item)


class _SlowClient:
    """Return one reusable response slowly so callers overlap in flight."""

    def __init__(
        self,
        response: _FakeResponse,
        *,
        delay_s: float = 0.2,
        release: threading.Event | None = None,
    ) -> None:
        self.response = response
        self.delay_s = delay_s
        self.release = release
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
        with self._lock:
            self.calls.append((url, params))
        self.entered.set()
        if self.release is None:
            time.sleep(self.delay_s)
        else:
            assert self.release.wait(timeout=2)
        return self.response


class _SeriesLimitResponse(_FakeResponse):
    """Loki's max_query_series rejection (400) — raise_for_status raises the
    real exception type the gateway sees (httpx.HTTPStatusError), carrying
    the response text the fallback inspects."""

    def __init__(self) -> None:
        super().__init__({}, status=400)
        self.text = "maximum number of series (500) reached for a single query"

    def raise_for_status(self) -> None:
        import httpx

        raise httpx.HTTPStatusError(
            "Client error '400 Bad Request'",
            request=httpx.Request("GET", "http://loki"),  # type: ignore[arg-type]
            response=self,  # type: ignore[arg-type]
        )


def _accessor(client: object) -> Any:
    """`loki_events._client` replacement: hands back the fake."""

    def _get() -> Any:
        return client

    return _get


def _install(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, Any] | _FakeResponse] | dict[str, Any],
) -> _FakeClient:
    client = _FakeClient(payloads)
    monkeypatch.setattr(loki_events, "_client", _accessor(client))
    return client


@pytest.fixture(autouse=True)
def _fresh_query_budget() -> Any:
    loki_query_budget.reset_for_tests()
    yield
    loki_query_budget.reset_for_tests()


@pytest.fixture(autouse=True)
def _fresh_aggregation_cache() -> Any:
    loki_events_cache.clear()
    yield
    loki_events_cache.clear()


@pytest.fixture(autouse=True)
def _stable_default_read_era(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep non-rollout tests independent of the wall-clock cutover date."""

    def _single_legacy(window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
        return (LokiReadSlice(LokiReadEra.LEGACY, *window),)

    monkeypatch.setattr(loki_events, "_read_slices", _single_legacy)


def test_aggregation_cache_key_is_canonical_and_minute_aligned() -> None:
    first = loki_events_cache.make_key(
        "shape",
        {"z": None, "filters": {"b": 2, "a": 1}, "names": ["turn_end"]},
        datetime(2026, 8, 1, 0, 0, 5, tzinfo=UTC),
        datetime(2026, 8, 1, 1, 0, 5, tzinfo=UTC),
    )
    second = loki_events_cache.make_key(
        "shape",
        {"names": ["turn_end"], "filters": {"a": 1, "b": 2}},
        datetime(2026, 8, 1, 0, 0, 55, tzinfo=UTC),
        datetime(2026, 8, 1, 1, 0, 55, tzinfo=UTC),
    )

    assert first == second


def test_aggregation_cache_expires_at_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(loki_events_cache.time, "monotonic", lambda: now)
    key = ("shape", "params", 1, 2)

    loki_events_cache.put(key, 42)
    assert loki_events_cache.get(key) == 42
    now += loki_events_cache.TTL_S
    assert loki_events_cache.get(key) is None


def test_aggregation_cache_clears_at_entry_cap() -> None:
    for value in range(1025):
        loki_events_cache.put((value,), value)

    assert loki_events_cache.get((0,)) is None
    assert loki_events_cache.get((1023,)) is None
    assert loki_events_cache.get((1024,)) == 1024


def test_aggregation_cache_clear_detaches_inflight_without_stranding_waiters() -> None:
    key = ("shape", "params", 1, 2)
    holder, is_leader = loki_events_cache.begin(key)
    waiter, waiter_is_leader = loki_events_cache.begin(key)

    assert is_leader is True
    assert waiter_is_leader is False
    assert waiter is holder

    loki_events_cache.clear()
    replacement, replacement_is_leader = loki_events_cache.begin(key)
    assert replacement_is_leader is True
    assert replacement is not holder

    loki_events_cache.finish(key, holder, value=42)
    assert waiter.event.wait(timeout=0.1)
    assert waiter.value == 42

    loki_events_cache.finish(key, replacement, value=43)


def _loki_payload(lines: list[tuple[str, str]]) -> dict[str, Any]:
    """One stream with (ts_ns_str, line) values, the shape Loki returns."""
    return {"data": {"result": [{"stream": {}, "values": [[ts, line] for ts, line in lines]}]}}


def _event_line(
    *, ts: str = "2026-08-12T00:00:00Z", agent_id: int | None = 7, **extra: object
) -> str:
    body: dict[str, object] = {
        "ts": ts,
        "trace_id": None,
        "span_id": None,
        "agent_id": agent_id,
        "machine": "machine-1",
        "process": "gateway",
        "category": "telemetry",
        "event_name": "llm_usage",
        "level": "info",
        "source": "test",
        "target_agent_id": None,
        "attributes": {"msg": "hello"},
    }
    body.update(extra)
    return json.dumps(body, separators=(",", ":"))


_ROLL_OUT_START = datetime(2026, 8, 10, tzinfo=UTC)
_ROLL_OUT_CUTOVER = _ROLL_OUT_START + timedelta(hours=1)
_ROLL_OUT_END = _ROLL_OUT_CUTOVER + timedelta(hours=1)


def _straddled_slices() -> tuple[LokiReadSlice, LokiReadSlice]:
    """Two non-overlapping label eras for cutover merge tests."""
    return (
        LokiReadSlice(LokiReadEra.LEGACY, _ROLL_OUT_START, _ROLL_OUT_CUTOVER),
        LokiReadSlice(LokiReadEra.INDEXED, _ROLL_OUT_CUTOVER, _ROLL_OUT_END),
    )


def _wait_for_budget_waiters(expected: int) -> None:
    """Synchronize concurrency tests on the queue state, never wall time."""
    budget: Any = loki_query_budget.query_budget
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with budget._condition:
            if len(budget._queue) == expected:
                return
        time.sleep(0.001)
    pytest.fail(f"Loki budget did not reach {expected} waiters")


class TestGlobalQueryBudget:
    def test_budget_contract_is_reexported_from_shared(self) -> None:
        spec = importlib.util.find_spec("shared.loki_query_budget")
        assert spec is not None, "shared.loki_query_budget must own the reusable budget contract"
        shared_budget = importlib.import_module("shared.loki_query_budget")
        for name in (
            "BudgetErrorFactory",
            "BudgetMetrics",
            "BudgetObservation",
            "BudgetObserver",
            "BudgetOutcome",
            "BudgetRejectReason",
            "FairQueryBudget",
            "LokiQueryBudgetError",
        ):
            assert getattr(loki_query_budget, name) is getattr(shared_budget, name)

    def test_matches_loki_real_max_concurrent(self) -> None:
        repo = Path(__file__).parents[2]
        configs = [
            yaml.safe_load((repo / path).read_text())
            for path in ("deploy/lgtm/config/loki.yaml", "deploy/lgtm/native/config/loki.yaml")
        ]
        retention = f"{int(EVENT_STREAM_RETENTION.total_seconds() // 3600)}h"
        for config in configs:
            assert config["querier"]["max_concurrent"] == LOKI_QUERY_CONCURRENCY
            assert config["limits_config"]["retention_period"] == retention
            assert config["limits_config"]["max_query_series"] == LOKI_MAX_QUERY_SERIES
            assert config["ingester"]["wal"]["disk_full_threshold"] == WAL_DISK_FULL_THRESHOLD
            validate_loki_deploy_config(config)
        assert loki_query_budget.LOKI_QUERY_CONCURRENCY == LOKI_QUERY_CONCURRENCY

    def test_both_loki_configs_retain_the_lineage_class_permanently(self) -> None:
        """Lineage rows outlive the global 84h bucket in BOTH deploy variants.

        The container config is the operator's manual rollback asset, so a rule
        that lands only in the native template would silently drop lineage back
        to 84 hours the moment the rollback path is used — the 2026-08-20 loss
        shape, one deploy variant later.
        """
        repo = Path(__file__).parents[2]
        # `set(...)` at the source: `_selector_event_names` yields `set[str]`, and
        # comparing that against a bare frozenset is a static no-overlap error.
        expected = set(lineage_event_names())
        assert expected == {
            "spawn",
            "fork",
            "resurrect",
            "agent_spawned",
            "agent_resurrected",
        }
        for path in ("deploy/lgtm/config/loki.yaml", "deploy/lgtm/native/config/loki.yaml"):
            limits = yaml.safe_load((repo / path).read_text())["limits_config"]
            rules = limits["retention_stream"]
            lineage = [r for r in rules if _selector_event_names(r["selector"]) == expected]
            assert len(lineage) == 1, f"{path} carries no single lineage retention_stream rule"
            assert lineage[0]["period"] == LINEAGE_RETENTION_PERIOD
            # The archive rule and the global bucket are untouched by this class.
            archive = [r for r in rules if r["selector"] == '{stream="archive"}']
            assert [r["period"] for r in archive] == ["8760h"]
            assert limits["retention_period"] == "84h"

    def test_rejects_loki_deploy_config_drift(self) -> None:
        with pytest.raises(ValueError, match="retention_period"):
            validate_loki_deploy_config(
                {
                    "limits_config": {
                        "retention_period": "96h",
                        "max_query_series": 20000,
                    },
                    "querier": {"max_concurrent": 8},
                }
            )
        with pytest.raises(ValueError, match="max_concurrent"):
            validate_loki_deploy_config(
                {
                    "limits_config": {
                        "retention_period": "84h",
                        "max_query_series": 20000,
                    },
                    "querier": {"max_concurrent": 8},
                }
            )
        with pytest.raises(ValueError, match="disk_full_threshold"):
            validate_loki_deploy_config(
                {
                    "limits_config": {
                        "retention_period": "84h",
                        "max_query_series": 20000,
                    },
                    "querier": {"max_concurrent": 4},
                    "ingester": {"wal": {"disk_full_threshold": 0.9}},
                }
            )
        with pytest.raises(ValueError, match="max_query_series"):
            validate_loki_deploy_config(
                {
                    "limits_config": {
                        "retention_period": "84h",
                        "max_query_series": 2000,
                    },
                    "querier": {"max_concurrent": 4},
                    "ingester": {"wal": {"disk_full_threshold": 0.95}},
                }
            )

    def test_rejects_lineage_retention_drift(self) -> None:
        """A config that lost the lineage rule, its period, or a name is drift.

        Every other pin here guards a limit; this one guards data that cannot be
        recreated. Retention lives in the deploy template while the class lives
        in the event registry, so nothing but this check couples them — which is
        precisely the gap the 2026-08-20 archive loss fell through.
        """

        def config(retention_stream: list[dict[str, object]]) -> dict[str, object]:
            return {
                "limits_config": {
                    "retention_period": "84h",
                    "max_query_series": LOKI_MAX_QUERY_SERIES,
                    "retention_stream": retention_stream,
                },
                "querier": {"max_concurrent": LOKI_QUERY_CONCURRENCY},
                "ingester": {"wal": {"disk_full_threshold": WAL_DISK_FULL_THRESHOLD}},
            }

        names = sorted(lineage_event_names())
        lineage_rule: dict[str, object] = {
            "selector": f'{{event_name=~"{"|".join(names)}"}}',
            "priority": 1,
            "period": LINEAGE_RETENTION_PERIOD,
        }
        archive_rule: dict[str, object] = {
            "selector": '{stream="archive"}',
            "priority": 1,
            "period": "8760h",
        }
        validate_loki_deploy_config(config([archive_rule, lineage_rule]))

        # The rule is missing entirely — the shape the archive loss shipped in.
        with pytest.raises(ValueError, match="lineage rule"):
            validate_loki_deploy_config(config([archive_rule]))
        # A lineage name was registered but never added to the deployed selector.
        with pytest.raises(ValueError, match="retention_class='lineage'"):
            validate_loki_deploy_config(
                config([{**lineage_rule, "selector": f'{{event_name=~"{"|".join(names[1:])}"}}'}])
            )
        # The period drifted back to a finite window.
        with pytest.raises(ValueError, match="lineage retention_stream period"):
            validate_loki_deploy_config(config([{**lineage_rule, "period": "8760h"}]))
        # Selector order is not drift: Loki's label regex is anchored, not ordered.
        reversed_rule: dict[str, object] = {
            **lineage_rule,
            "selector": f'{{event_name=~"{"|".join(names[::-1])}"}}',
        }
        validate_loki_deploy_config(config([reversed_rule]))

    def test_loki_preserves_default_resource_labels_and_indexes_event_dimensions(self) -> None:
        config_path = Path(__file__).parents[2] / "deploy/lgtm/config/loki.yaml"
        config = yaml.safe_load(config_path.read_text())
        labels = config["distributor"]["otlp_config"]["default_resource_attributes_as_index_labels"]
        assert labels == [
            "service.name",
            "service.namespace",
            "service.instance.id",
            "deployment.environment",
            "deployment.environment.name",
            "cloud.region",
            "cloud.availability_zone",
            "k8s.cluster.name",
            "k8s.namespace.name",
            "k8s.pod.name",
            "k8s.container.name",
            "container.name",
            "k8s.replicaset.name",
            "k8s.deployment.name",
            "k8s.statefulset.name",
            "k8s.daemonset.name",
            "k8s.cronjob.name",
            "k8s.job.name",
            "agent_id",
            "event_name",
        ]

    def test_observes_every_transition_and_types_local_rejections(self) -> None:
        """Saturation is observable and distinguishable without touching Loki.

        The observer runs after the budget lock is released; a monitoring
        callback therefore cannot deadlock the state machine. Rejection
        reasons are typed so routers can map local capacity to 503 without
        misclassifying it as a Loki transport failure.
        """
        observations: list[Any] = []
        budget_ref: list[Any] = []

        def observe(observation: Any) -> None:
            budget = budget_ref[0]
            assert budget._condition.acquire(blocking=False)
            budget._condition.release()
            observations.append(observation)

        budget = loki_query_budget.FairQueryBudget(
            capacity=1,
            max_waiters=1,
            wait_timeout_s=0.05,
            observer=observe,
        )
        budget_ref.append(budget)
        holder_entered = threading.Event()
        release_holder = threading.Event()

        def hold_slot() -> None:
            with budget.slot():
                holder_entered.set()
                assert release_holder.wait(timeout=2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(hold_slot)
            assert holder_entered.wait(timeout=1)
            waiter = executor.submit(lambda: budget.slot().__enter__())
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if budget.metrics().queued == 1:
                    break
                time.sleep(0.001)
            else:
                pytest.fail("waiter never entered the budget queue")
            with (
                pytest.raises(loki_query_budget.LokiQueryBudgetError) as overflow,
                budget.slot(),
            ):
                pass
            assert overflow.value.reason == "queue_full"
            with pytest.raises(loki_query_budget.LokiQueryBudgetError) as timed_out:
                waiter.result(timeout=1)
            assert timed_out.value.reason == "acquire_timeout"
            release_holder.set()
            holder.result(timeout=1)

        metrics = budget.metrics()
        assert metrics.active == 0
        assert metrics.queued == 0
        assert metrics.high_water == 1
        assert metrics.acquired == 1
        assert metrics.queue_full == 1
        assert metrics.wait_timeout == 1
        assert any(item.outcome == "acquired" and item.acquired == 1 for item in observations)
        assert any(item.outcome == "queue_full" and item.queue_full == 1 for item in observations)
        assert any(
            item.outcome == "wait_timeout" and item.wait_timeout == 1 for item in observations
        )
        assert observations[-1].outcome == "released"
        assert observations[-1].active == 0

    def test_gateway_telemetry_emits_only_budget_pressure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Routine acquire/release transitions stay out of the event stream."""
        emitted: list[dict[str, Any]] = []

        def capture(*_args: Any, **kwargs: Any) -> None:
            emitted.append(kwargs["attributes"])

        monkeypatch.setattr(loki_query_budget.telemetry, "emit", capture)
        budget = loki_query_budget.FairQueryBudget(
            capacity=1,
            max_waiters=0,
            wait_timeout_s=1.0,
            observer=loki_query_budget._emit_observation,
        )

        with (
            budget.slot(),
            pytest.raises(loki_query_budget.LokiQueryBudgetError) as rejected,
            budget.slot(),
        ):
            pass

        assert rejected.value.reason == "queue_full"
        assert [item["outcome"] for item in emitted] == ["queue_full"]

    def test_local_budget_rejection_is_not_logged_as_loki_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = threading.Event()
        entered = threading.Event()
        failures: list[dict[str, Any]] = []

        class BlockingClient:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                entered.set()
                assert gate.wait(timeout=2)
                return _FakeResponse({"data": {"result": []}})

        monkeypatch.setattr(loki_events, "_client", _accessor(BlockingClient()))

        def log_failure(**kwargs: Any) -> None:
            failures.append(kwargs)

        monkeypatch.setattr(loki_events, "_log_loki_failure", log_failure)
        loki_query_budget.reset_for_tests(capacity=1, max_waiters=1, wait_timeout_s=1.0)
        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(
                loki_events._get_json,
                "http://loki/query",
                {"query": "holder"},
                endpoint="query",
            )
            assert entered.wait(timeout=1)
            waiter = executor.submit(
                loki_events._get_json,
                "http://loki/query",
                {"query": "waiter"},
                endpoint="query",
            )
            _wait_for_budget_waiters(1)
            with pytest.raises(loki_query_budget.LokiQueryBudgetError) as rejected:
                loki_events._get_json("http://loki/query", {"query": "overflow"}, endpoint="query")
            assert rejected.value.reason == "queue_full"
            assert failures == []
            gate.set()
            holder.result(timeout=1)
            waiter.result(timeout=1)

    def test_caps_all_loki_http_calls_at_four(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gate = threading.Event()
        state_lock = threading.Lock()
        active_peak: list[int] = [0, 0]

        class BlockingClient:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                with state_lock:
                    active_peak[0] += 1
                    active_peak[1] = max(active_peak)
                assert gate.wait(timeout=2)
                with state_lock:
                    active_peak[0] -= 1
                return _FakeResponse({"data": {"result": []}})

        monkeypatch.setattr(loki_events, "_client", _accessor(BlockingClient()))
        loki_query_budget.reset_for_tests(capacity=4, wait_timeout_s=1.0)
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [
                executor.submit(
                    loki_events._get_json,
                    "http://loki/query",
                    {"query": f"q-{i}"},
                    endpoint="query",
                )
                for i in range(12)
            ]
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with state_lock:
                    if active_peak[1] == 4:
                        break
                time.sleep(0.001)
            assert active_peak[1] == 4
            gate.set()
            assert all(future.result(timeout=2) == {"data": {"result": []}} for future in futures)

    def test_wait_timeout_and_transport_error_release_capacity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = threading.Event()
        entered = threading.Event()
        calls = 0

        class SequencedClient:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                nonlocal calls
                calls += 1
                if calls == 1:
                    entered.set()
                    assert gate.wait(timeout=2)
                if params["query"] == "transport-error":
                    raise httpx.ReadTimeout("loki read timeout")
                return _FakeResponse({"data": {"result": []}})

        monkeypatch.setattr(loki_events, "_client", _accessor(SequencedClient()))
        loki_query_budget.reset_for_tests(capacity=1, wait_timeout_s=0.05)
        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(
                loki_events._get_json,
                "http://loki/query",
                {"query": "holder"},
                endpoint="query",
            )
            assert entered.wait(timeout=1)
            with pytest.raises(loki_query_budget.LokiQueryBudgetError) as timeout:
                loki_events._get_json("http://loki/query", {"query": "queued"}, endpoint="query")
            assert timeout.value.reason == "acquire_timeout"
            gate.set()
            holder.result(timeout=1)

        with pytest.raises(httpx.ReadTimeout):
            loki_events._get_json(
                "http://loki/query", {"query": "transport-error"}, endpoint="query"
            )

        class CancelThenSucceed:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                if params["query"] == "cancelled":
                    raise asyncio.CancelledError
                return _FakeResponse({"data": {"result": []}})

        monkeypatch.setattr(loki_events, "_client", _accessor(CancelThenSucceed()))
        with pytest.raises(asyncio.CancelledError):
            loki_events._get_json("http://loki/query", {"query": "cancelled"}, endpoint="query")
        assert loki_events._get_json(
            "http://loki/query", {"query": "after-errors"}, endpoint="query"
        ) == {"data": {"result": []}}
        metrics = loki_query_budget.query_budget.metrics()
        assert metrics.active == 0
        assert metrics.queued == 0
        # holder, transport failure, cancellation, and final success all
        # acquired then released the only slot.
        assert metrics.acquired == 4
        assert metrics.wait_timeout == 1

    def test_wait_queue_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gate = threading.Event()
        entered = threading.Event()

        class BlockingClient:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                entered.set()
                assert gate.wait(timeout=2)
                return _FakeResponse({"data": {"result": []}})

        monkeypatch.setattr(loki_events, "_client", _accessor(BlockingClient()))
        loki_query_budget.reset_for_tests(capacity=1, max_waiters=1, wait_timeout_s=1.0)
        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(
                loki_events._get_json,
                "http://loki/query",
                {"query": "holder"},
                endpoint="query",
            )
            assert entered.wait(timeout=1)
            waiter = executor.submit(
                loki_events._get_json,
                "http://loki/query",
                {"query": "waiter"},
                endpoint="query",
            )
            _wait_for_budget_waiters(1)
            with pytest.raises(httpx.PoolTimeout, match="queue is full"):
                loki_events._get_json("http://loki/query", {"query": "overflow"}, endpoint="query")
            gate.set()
            holder.result(timeout=1)
            waiter.result(timeout=1)

    def test_fifo_lets_stats_run_before_an_inspect_worker_reacquires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = threading.Event()
        inspect_started = threading.Event()
        order: list[str] = []
        order_lock = threading.Lock()

        class OrderedClient:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                query = str(params["query"])
                with order_lock:
                    order.append(query)
                if query == "holder":
                    assert gate.wait(timeout=2)
                return _FakeResponse({"data": {"result": []}})

        def inspect_chain() -> None:
            inspect_started.set()
            loki_events._get_json("http://loki/query", {"query": "inspect-1"}, endpoint="query")
            loki_events._get_json("http://loki/query", {"query": "inspect-2"}, endpoint="query")

        monkeypatch.setattr(loki_events, "_client", _accessor(OrderedClient()))
        loki_query_budget.reset_for_tests(capacity=1, wait_timeout_s=1.0)
        with ThreadPoolExecutor(max_workers=3) as executor:
            holder = executor.submit(
                loki_events._get_json,
                "http://loki/query",
                {"query": "holder"},
                endpoint="query",
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with order_lock:
                    if order == ["holder"]:
                        break
                time.sleep(0.001)
            else:
                pytest.fail("holder never entered the Loki client")
            inspect = executor.submit(inspect_chain)
            assert inspect_started.wait(timeout=1)
            _wait_for_budget_waiters(1)
            stats = executor.submit(
                loki_events._get_json,
                "http://loki/query",
                {"query": "stats"},
                endpoint="query",
            )
            _wait_for_budget_waiters(2)
            gate.set()
            holder.result(timeout=1)
            inspect.result(timeout=1)
            stats.result(timeout=1)

        assert order == ["holder", "inspect-1", "stats", "inspect-2"]


# ─── _build_logql ────────────────────────────────────────────────────────────


class TestLiveArchiveExclusion:
    def test_shared_selector_excludes_archive_rows(self) -> None:
        """The 2026-08 archive proves the shared selector's real invariant."""
        url = loki_events.settings.observability.telemetry_loki_url.rstrip("/")
        try:
            ready = httpx.get(f"{url}/ready", timeout=3)
            ready.raise_for_status()
        except httpx.HTTPError as exc:
            pytest.skip(f"Loki unavailable for archive exclusion invariant: {exc}")

        start = datetime(2026, 8, 1, tzinfo=UTC)
        end = datetime(2026, 8, 10, tzinfo=UTC)

        def count(selector: str) -> int:
            response = httpx.get(
                f"{url}/loki/api/v1/query",
                params={
                    "query": f"sum(count_over_time({selector}[{int((end - start).total_seconds())}s]))",
                    "time": end.timestamp(),
                },
                timeout=10,
            )
            response.raise_for_status()
            result = response.json().get("data", {}).get("result", [])
            return int(float(result[0]["value"][1])) if result else 0

        archive_rows = count('{service_name="unknown_service", stream="archive"}')
        if archive_rows == 0:
            pytest.skip("no archive rows in this Loki")

        selector = event_stream_selector(
            era=LokiReadEra.LEGACY,
            agent_id=None,
            event_names=None,
        )
        assert count(selector) == 0


class TestBuildLogql:
    def test_seeded_filter_values_preserve_quoting_and_pipeline_order(self) -> None:
        """Random filter text must stay inside its quoted LogQL stages.

        This catches a broken escape or a reordered `| json` stage, either of
        which changes the query language rather than merely its formatting.
        """
        rng = random.Random(20260901)  # noqa: S311 — deterministic property inputs
        alphabet = 'abC19 \\"\n\r|=()[]{}'
        for _ in range(100):
            value = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))
            escaped = (
                value.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", " ")
                .replace("\r", " ")
            )
            query = _loki_logql._build_logql(
                grep=value,
                cluster=value,
                attribute_filters={"attribute": value},
            )

            assert loki_events._build_logql is _loki_logql._build_logql
            assert query.index(f'|= "{escaped}"') < query.index("| json")
            assert f'| cluster="{escaped}" or cluster=""' in query
            assert '| json attribute="attributes.attribute"' in query
            assert f'| attribute="{escaped}"' in query

    def test_default_is_selector_plus_json(self) -> None:
        assert (
            loki_events._build_logql()
            == '{service_name="unknown_service", stream!="archive"} | json'
        )

    def test_agent_id_filter(self) -> None:
        q = loki_events._build_logql(agent_id=42)
        # Body fields are authoritative when structured metadata was promoted
        # from a different record in the same OTLP batch (task #1515).
        assert '| agent_id_extracted="42"' in q
        assert '| agent_id="42"' not in q
        assert "| json" in q

    def test_cluster_filter_follows_json_stage(self) -> None:
        q = loki_events._build_logql(cluster=".ava-preview")
        assert q == (
            '{service_name="unknown_service", stream!="archive"} | json | cluster=".ava-preview" or cluster=""'
        )

    def test_cluster_filter_keeps_unlabeled_history_in_aggregation_pipeline(self) -> None:
        q = loki_events._agg_pipeline(cluster=".ava-preview")

        assert (
            q
            == '{service_name="unknown_service", stream!="archive"} | cluster=".ava-preview" or cluster=""'
        )

    def test_count_grouped_cluster_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch,
            {"data": {"result": [{"metric": {"level": "warning"}, "value": [1, "2"]}]}},
        )

        assert loki_events.count_grouped(group_by="level", cluster=".ava-preview") == {"warning": 2}
        assert 'cluster=".ava-preview" or cluster=""' in client.calls[0][1]["query"]

    def test_indexed_selector_narrows_before_pipeline_filters(self) -> None:
        q = loki_events._build_logql(
            era=LokiReadEra.INDEXED,
            agent_id=42,
            event_names=["spawn", "terminate"],
        )
        assert q.startswith(
            '{service_name="unknown_service", stream!="archive", agent_id="42", event_name=~"spawn|terminate"}'
        )
        assert '| agent_id_extracted="42"' in q
        assert '| event_name_extracted=~"spawn|terminate"' in q
        assert '| agent_id="42"' not in q
        assert '| event_name=~"spawn|terminate"' not in q

    def test_service_only_matches_null_agent_id(self) -> None:
        # json turns a JSON null into an absent field; empty-string matches it.
        # The live stream's extracted field carries the `_extracted` suffix
        # (the index label collides); service rows match the empty extraction.
        assert '| agent_id_extracted=""' in loki_events._build_logql(service_only=True)

    def test_grep_is_a_line_filter_before_json(self) -> None:
        q = loki_events._build_logql(grep="boom")
        assert q.startswith('{service_name="unknown_service", stream!="archive"} |= "boom" | json')

    def test_categories_become_or_regex(self) -> None:
        q = loki_events._build_logql(categories=["telemetry", "log"])
        assert '| category=~"telemetry|log"' in q

    def test_event_names_become_or_regex(self) -> None:
        q = loki_events._build_logql(event_names=["spawn", "terminate"])
        assert '| event_name_extracted=~"spawn|terminate"' in q
        assert '| event_name=~"spawn|terminate"' not in q

    def test_archive_selector_and_plain_fields(self) -> None:
        """The archive stream (task #1281) has no event_name/agent_id index
        labels: the selector targets stream=archive and the filters match the
        plain json-extracted fields (no `_extracted` suffix)."""
        q = loki_events._build_logql(archive=True, agent_id=42, event_names=["spawn"])
        assert q.startswith('{service_name="unknown_service", stream="archive"} | json')
        assert '| agent_id="42"' in q
        assert '| event_name=~"spawn"' in q
        assert "agent_id_extracted" not in q
        assert "event_name_extracted" not in q
        assert 'stream!="archive"' not in q

    def test_archive_query_events_bounds_one_slice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """archive=True must not split the read at the live stream's index-label
        cutover — the archive is one era, queried as a single slice."""
        client = _install(monkeypatch, {"data": {"result": []}})
        loki_events.query_events(
            archive=True,
            event_names=["spawn"],
            from_=datetime(2026, 5, 24, tzinfo=UTC),
            to=datetime(2026, 8, 13, tzinfo=UTC),
        )
        assert len(client.calls) == 1
        query = client.calls[0][1]["query"]
        assert query.startswith('{service_name="unknown_service", stream="archive"} | json')

    def test_level_min_is_a_threshold_regex(self) -> None:
        q = loki_events._build_logql(level_min="warning")
        assert '| level=~"warning|error|critical"' in q

    def test_level_exact(self) -> None:
        q = loki_events._build_logql(level="warning")
        assert '| level="warning"' in q
        assert "=~" not in q

    def test_noise_tier_matches_only_ordinary_noise_rows(self) -> None:
        q = loki_events._build_logql(tiers=["noise"])
        assert 'level!~"warning|error|critical" and category!="audit"' in q
        assert 'event_name=~"' in q
        assert "node_exit" in q

    def test_observation_tier_excludes_noise_and_anomaly_event_names(self) -> None:
        q = loki_events._build_logql(tiers=["observation"])
        assert 'level!~"warning|error|critical" and category!="audit"' in q
        assert 'event_name!~"' in q
        assert "node_exit" in q
        assert "exec_failed" in q

    def test_machine_and_trace_id(self) -> None:
        q = loki_events._build_logql(machine="machine-1", trace_id="ABCD")
        assert '| machine="machine-1"' in q
        assert '| trace_id="abcd"' in q  # lowercased

    def test_escapes_quotes_and_backslashes(self) -> None:
        q = loki_events._build_logql(grep='say "hi" \\n')
        assert '|= "say \\"hi\\" \\\\n"' in q
        q2 = loki_events._build_logql(grep="line1\nline2")
        assert '|= "line1 line2"' in q2


class TestObservabilityReadGate:
    def test_non_lgtm_gateway_rejects_default_loki_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / ".ava-preview"
        home.mkdir()
        monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
        monkeypatch.setattr("shared.paths.ava_home", lambda: home)
        monkeypatch.delitem(os.environ, "AVA_TELEMETRY_LOKI_URL", raising=False)

        with pytest.raises(
            loki_events.ObservabilityReadUnavailable,
            match="AVA_TELEMETRY_LOKI_URL",
        ):
            loki_events._read_gate()

    @pytest.mark.parametrize("override", ["marker", "environment", "runner"])
    def test_read_gate_allows_explicit_or_non_gateway_topology(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        override: str,
    ) -> None:
        home = tmp_path / ".ava-preview"
        home.mkdir()
        monkeypatch.setattr("shared.paths.ava_home", lambda: home)
        monkeypatch.delitem(os.environ, "AVA_TELEMETRY_LOKI_URL", raising=False)
        if override == "marker":
            (home / "lgtm-host").touch()
            monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
        elif override == "environment":
            monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
            monkeypatch.setitem(os.environ, "AVA_TELEMETRY_LOKI_URL", "http://loki.invalid:3100")
        else:
            monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"agent-runner"}))

        loki_events._read_gate()


# ─── _parse_line ─────────────────────────────────────────────────────────────


class TestParseLine:
    def test_full_round_trip(self) -> None:
        line = _event_line()
        row = loki_events._parse_line(line, 1_000_000)
        assert row is not None
        assert row["agent_id"] == 7
        assert row["event_name"] == "llm_usage"
        assert row["level"] == "info"
        assert row["attributes"] == {"msg": "hello"}
        assert row["ts"].tzinfo is not None
        assert isinstance(row["id"], int)
        # stable: same line + ts -> same id
        second = loki_events._parse_line(line, 1_000_000)
        assert second is not None
        assert row["id"] == second["id"]

    def test_bad_json_skipped(self) -> None:
        assert loki_events._parse_line("not json", 1) is None
        assert loki_events._parse_line("42", 1) is None  # non-dict JSON

    def test_bad_ts_falls_back_to_loki_timestamp(self) -> None:
        line = _event_line(ts="garbage")
        row = loki_events._parse_line(line, 1_720_000_000_000_000_000)
        assert row is not None
        assert row["ts"] == datetime.fromtimestamp(1_720_000_000, UTC)

    def test_missing_ts_uses_loki_timestamp(self) -> None:
        line = _event_line()
        # rebuild without ts
        body = json.loads(line)
        body.pop("ts")
        line2 = json.dumps(body)
        row = loki_events._parse_line(line2, 1_720_000_000_000_000_000)
        assert row is not None
        assert row["ts"] == datetime.fromtimestamp(1_720_000_000, UTC)

    def test_level_lowercased_and_defaults(self) -> None:
        row = loki_events._parse_line(_event_line(level="ERROR", agent_id=None), 1)
        assert row is not None
        assert row["level"] == "error"
        assert row["agent_id"] is None
        assert row["machine"] == "machine-1"


# ─── query_events (httpx mocked) ─────────────────────────────────────────────


class TestQueryEvents:
    def test_per_call_timeout_overrides_shared_client_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timeouts: list[float] = []

        class _TimedClient:
            def get(self, url: str, params: dict[str, Any], *, timeout: float) -> _FakeResponse:
                timeouts.append(timeout)
                return _FakeResponse(_loki_payload([]))

        monkeypatch.setattr(loki_events, "_client", _accessor(_TimedClient()))

        def _slices(_window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
            return _straddled_slices()

        monkeypatch.setattr(loki_events, "_read_slices", _slices)

        loki_events.query_events(from_=_ROLL_OUT_START, to=_ROLL_OUT_END, timeout_s=8.0)

        # era-sliced read path: the straddling window spans the legacy and
        # indexed slices (task #1407 B2), each carrying the timeout
        assert timeouts == [8.0, 8.0]

    def test_request_params_and_straddling_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _loki_payload([]))

        def _slices(_window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
            return _straddled_slices()

        monkeypatch.setattr(loki_events, "_read_slices", _slices)
        rows, has_more = loki_events.query_events(
            agent_id=3, limit=100, offset=0, from_=_ROLL_OUT_START, to=_ROLL_OUT_END
        )
        assert rows == []
        assert has_more is False
        url, params = client.calls[0]
        assert url.endswith("/loki/api/v1/query_range")
        assert (
            params["query"] == '{service_name="unknown_service", stream!="archive"} | json '
            '| agent_id_extracted="3"'
        )
        assert params["direction"] == "backward"
        assert params["limit"] == 101  # limit + offset + 1 lookahead
        # explicit straddling window: the indexed slice spans cutover -> end
        _, last_params = client.calls[-1]
        assert last_params["start"] == int(_ROLL_OUT_CUTOVER.timestamp() * 1e9)
        assert last_params["end"] == int(_ROLL_OUT_END.timestamp() * 1e9)

    def test_tier_filter_drops_json_parse_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _loki_payload([]))

        loki_events.query_events(tiers=["noise"])

        assert '| __error__=""' in client.calls[0][1]["query"]

    def test_metric_range_parses_matrix_values_as_seconds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Matrix range-vector values come back in unix SECONDS; the bucketed
        series must not divide them by 1e9 (regression: every bucket folded
        to 1970)."""
        payload: dict[str, Any] = {
            "data": {
                "result": [
                    {
                        "metric": {},
                        "values": [
                            ["1723300000", "1.5"],
                            ["1723300300", "2.5"],
                        ],
                    }
                ]
            }
        }
        _install(monkeypatch, payload)
        rows = loki_events.metric_range(
            'max(max_over_time(({service_name=~".+"}[300s])))',
            from_=datetime(2026, 8, 10, tzinfo=UTC),
            to=datetime(2026, 8, 10, 1, tzinfo=UTC),
            step_s=300,
        )
        assert rows == [
            (datetime.fromtimestamp(1_723_300_000, UTC).isoformat(), 1.5),
            (datetime.fromtimestamp(1_723_300_300, UTC).isoformat(), 2.5),
        ]

    def test_explicit_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _loki_payload([]))
        from_ = datetime(2026, 8, 1, tzinfo=UTC)
        to = datetime(2026, 8, 2, tzinfo=UTC)
        loki_events.query_events(from_=from_, to=to)
        _, params = client.calls[0]
        assert params["start"] == int(from_.timestamp() * 1e9)
        assert params["end"] == int(to.timestamp() * 1e9)

    def test_straddle_merges_and_deduplicates_the_cutover_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = _event_line(ts="2026-08-10T00:30:00Z", event_name="before")
        labeled_before = _event_line(ts="2026-08-10T00:45:00Z", event_name="labeled_before")
        boundary = _event_line(ts="2026-08-10T01:00:00Z", event_name="boundary")
        after = _event_line(ts="2026-08-10T01:30:00Z", event_name="after")
        client = _install(
            monkeypatch,
            [
                _loki_payload(
                    [
                        ("1786309200000000000", before),
                        ("1786310100000000000", labeled_before),
                        ("1786311000000000000", boundary),
                    ]
                ),
                _loki_payload(
                    [
                        ("1786311000000000000", boundary),
                        ("1786312800000000000", after),
                    ]
                ),
            ],
        )

        def _slices(_window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
            return _straddled_slices()

        monkeypatch.setattr(loki_events, "_read_slices", _slices)

        rows, has_more = loki_events.query_events(
            agent_id=7,
            event_names=["spawn"],
            from_=_ROLL_OUT_START,
            to=_ROLL_OUT_END,
            direction="forward",
            limit=4,
        )

        assert [row["event_name"] for row in rows] == [
            "before",
            "labeled_before",
            "boundary",
            "after",
        ]
        assert has_more is False
        assert len(client.calls) == 2
        assert client.calls[0][1]["query"].startswith(
            '{service_name="unknown_service", stream!="archive"}'
        )
        assert 'event_name=""' not in client.calls[0][1]["query"]
        assert client.calls[1][1]["query"].startswith(
            '{service_name="unknown_service", stream!="archive", event_name!="", agent_id="7", event_name="spawn"}'
        )

    def test_newest_first_merge_across_streams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "data": {
                "result": [
                    {
                        "stream": {"a": "1"},
                        "values": [
                            ["1723300000000000001", _event_line(ts="2026-08-10T10:00:01Z")],
                            ["1723300000000000003", _event_line(ts="2026-08-10T10:00:03Z")],
                        ],
                    },
                    {
                        "stream": {"a": "2"},
                        "values": [
                            ["1723300000000000002", _event_line(ts="2026-08-10T10:00:02Z")],
                        ],
                    },
                ]
            }
        }
        _install(monkeypatch, payload)
        rows, _ = loki_events.query_events(limit=10)
        assert [r["ts"].isoformat() for r in rows] == [
            "2026-08-10T10:00:03+00:00",
            "2026-08-10T10:00:02+00:00",
            "2026-08-10T10:00:01+00:00",
        ]

    def test_offset_slices_in_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lines = [
            (str(1_723_000_000_000_000_000 + i), _event_line(ts=f"2026-08-10T10:00:0{i}Z"))
            for i in range(5)
        ]
        client = _install(monkeypatch, _loki_payload(lines))
        rows, has_more = loki_events.query_events(limit=2, offset=2)
        assert client.calls[0][1]["limit"] == 5  # 2 + 2 + 1
        assert len(rows) == 2
        assert has_more is True  # 5 fetched, 4 needed -> more behind
        # offset=2 -> rows 2..3 (newest first)
        assert rows[0]["ts"].second == 2
        assert rows[1]["ts"].second == 1

    def test_has_more_exact_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # exactly limit rows -> no more
        lines = [
            (str(1_723_000_000_000_000_000 + i), _event_line(ts=f"2026-08-10T10:00:0{i}Z"))
            for i in range(3)
        ]
        _install(monkeypatch, _loki_payload(lines))
        rows, has_more = loki_events.query_events(limit=3)
        assert len(rows) == 3 and has_more is False
        # limit+1 rows -> has_more True, only limit returned
        lines.append(("9999999999999999999", _event_line(ts="2026-08-10T10:00:09Z")))
        _install(monkeypatch, _loki_payload(lines))
        rows, has_more = loki_events.query_events(limit=3)
        assert len(rows) == 3 and has_more is True

    def test_unparseable_lines_do_not_consume_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-JSON line in the stream must be skipped, not counted against
        the page — the +1 lookahead is on *parsed* rows, so has_more stays
        exact even with junk in the stream."""
        lines = [
            ("1723300000000000001", "not json at all"),
            ("1723300000000000002", _event_line(ts="2026-08-10T10:00:02Z")),
            ("1723300000000000003", _event_line(ts="2026-08-10T10:00:03Z")),
        ]
        client = _install(monkeypatch, _loki_payload(lines))
        rows, has_more = loki_events.query_events(limit=1)
        assert client.calls[0][1]["limit"] == 2
        assert len(rows) == 1
        assert has_more is True

    def test_filters_forwarded_into_logql(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _loki_payload([]))
        loki_events.query_events(
            agent_id=5,
            service_only=False,
            categories=["telemetry", "log"],
            event_names=["spawn", "terminate"],
            level_min="warning",
            grep="boom",
            cluster=".ava-preview",
            machine="machine-1",
            trace_id="abc",
            limit=50,
        )
        q = client.calls[0][1]["query"]
        assert '| agent_id_extracted="5"' in q
        assert '| category=~"telemetry|log"' in q
        assert '| event_name_extracted=~"spawn|terminate"' in q
        assert '| level=~"warning|error|critical"' in q
        assert '|= "boom"' in q
        assert '| cluster=".ava-preview" or cluster=""' in q
        assert '| machine="machine-1"' in q
        assert '| trace_id="abc"' in q

    def test_http_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def get(self, url: str, params: dict) -> _FakeResponse:  # type: ignore[no-untyped-def]
                return _FakeResponse({}, status=500)

        monkeypatch.setattr(loki_events, "_client", _accessor(_Boom()))
        with pytest.raises(RuntimeError):
            loki_events.query_events()


# ─── count_events (httpx mocked) ─────────────────────────────────────────────


class TestCountEvents:
    def test_count_events_cache_hit_within_minute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch,
            {"data": {"result": [{"metric": {}, "value": [1723300000, "42"]}]}},
        )
        from_ = datetime(2026, 8, 1, 0, 0, 5, tzinfo=UTC)
        to = datetime(2026, 8, 1, 1, 0, 5, tzinfo=UTC)

        assert loki_events.count_events(event_names=["turn_end"], from_=from_, to=to) == 42
        assert (
            loki_events.count_events(
                event_names=["turn_end"],
                from_=from_ + timedelta(seconds=40),
                to=to + timedelta(seconds=40),
            )
            == 42
        )
        assert len(client.calls) == 1

    def test_count_events_deduplicates_concurrent_same_key_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _SlowClient(
            _FakeResponse({"data": {"result": [{"metric": {}, "value": [1, "42"]}]}})
        )
        monkeypatch.setattr(loki_events, "_client", _accessor(client))
        barrier = threading.Barrier(2)

        def count() -> int:
            barrier.wait(timeout=1)
            return loki_events.count_events(
                event_names=["turn_end"],
                from_=datetime(2026, 8, 1, tzinfo=UTC),
                to=datetime(2026, 8, 2, tzinfo=UTC),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=2) for future in [pool.submit(count) for _ in range(2)]
            ]

        assert results == [42, 42]
        assert len(client.calls) == 1

    def test_count_events_rechecks_cache_after_claiming_a_retired_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(
            monkeypatch,
            {"data": {"result": [{"metric": {}, "value": [1, "42"]}]}},
        )
        first_miss = threading.Event()
        resume_first = threading.Event()
        real_get = loki_events_cache.get
        get_count = 0
        get_lock = threading.Lock()

        def pause_first_miss(key: tuple[object, ...]) -> object | None:
            nonlocal get_count
            result = real_get(key)
            with get_lock:
                get_count += 1
                this_get = get_count
            if this_get == 1 and result is None:
                first_miss.set()
                assert resume_first.wait(timeout=2)
            return result

        monkeypatch.setattr(loki_events_cache, "get", pause_first_miss)

        def count() -> int:
            return loki_events.count_events(
                event_names=["turn_end"],
                from_=datetime(2026, 8, 1, tzinfo=UTC),
                to=datetime(2026, 8, 2, tzinfo=UTC),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            paused = pool.submit(count)
            assert first_miss.wait(timeout=1)
            try:
                assert pool.submit(count).result(timeout=2) == 42
            finally:
                resume_first.set()
            assert paused.result(timeout=2) == 42

        assert len(client.calls) == 1

    def test_count_events_cache_miss_on_new_minute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch,
            [
                {"data": {"result": [{"metric": {}, "value": [1723300000, "42"]}]}},
                {"data": {"result": [{"metric": {}, "value": [1723300120, "7"]}]}},
            ],
        )
        from_ = datetime(2026, 8, 1, tzinfo=UTC)
        to = datetime(2026, 8, 1, 1, tzinfo=UTC)

        assert loki_events.count_events(from_=from_, to=to) == 42
        assert (
            loki_events.count_events(
                from_=from_ + timedelta(seconds=120),
                to=to + timedelta(seconds=120),
            )
            == 7
        )
        assert len(client.calls) == 2

    def test_instant_query_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch, {"data": {"result": [{"metric": {}, "value": [1723300000, "42"]}]}}
        )
        n = loki_events.count_events(
            agent_id=7,
            categories=["telemetry", "log"],
            event_names=["spawn"],
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
        )
        assert n == 42
        _url, params = client.calls[0]
        assert _url.endswith("/loki/api/v1/query")  # instant endpoint
        q = params["query"]
        assert q.startswith("sum(count_over_time((")
        assert q.endswith(")[86400s]))")
        assert '| __error__=""' in q
        assert '| agent_id_extracted="7"' in q
        assert '| category=~"telemetry|log"' in q
        assert '| event_name_extracted=~"spawn"' in q
        assert params["time"] == datetime(2026, 8, 2, tzinfo=UTC).timestamp()

    def test_straddling_window_sums_both_slice_durations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})

        def _slices(_window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
            return _straddled_slices()

        monkeypatch.setattr(loki_events, "_read_slices", _slices)
        assert loki_events.count_events(from_=_ROLL_OUT_START, to=_ROLL_OUT_END) == 0
        # era-sliced read path: a straddling window splits into legacy +
        # indexed slices; their durations must sum to the window length (2h)
        assert len(client.calls) == 2
        total = 0
        for _url, params in client.calls:
            duration = int(params["query"].rsplit("[", 1)[1].split("s")[0])
            total += duration
        assert total == 7200

    def test_empty_window_returns_zero_without_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})
        n = loki_events.count_events(
            from_=datetime(2026, 8, 2, tzinfo=UTC),
            to=datetime(2026, 8, 1, tzinfo=UTC),
        )
        assert n == 0
        assert client.calls == []

    def test_empty_result_is_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, {"data": {"result": []}})
        assert loki_events.count_events() == 0

    def test_straddle_adds_disjoint_era_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch,
            [
                {"data": {"result": [{"metric": {}, "value": [0, "2"]}]}},
                {"data": {"result": [{"metric": {}, "value": [0, "3"]}]}},
            ],
        )

        def _slices(_window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
            return _straddled_slices()

        monkeypatch.setattr(loki_events, "_read_slices", _slices)

        assert (
            loki_events.count_events(
                agent_id=7,
                event_names=["spawn"],
                from_=_ROLL_OUT_START,
                to=_ROLL_OUT_END,
            )
            == 5
        )
        assert 'event_name=""' not in client.calls[0][1]["query"]
        assert 'event_name!="", agent_id="7", event_name="spawn"' in client.calls[1][1]["query"]

    def test_query_events_empty_window_returns_no_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})
        rows, has_more = loki_events.query_events(
            from_=datetime(2026, 8, 2, tzinfo=UTC),
            to=datetime(2026, 8, 1, tzinfo=UTC),
        )
        assert rows == [] and has_more is False
        assert client.calls == []


class TestInspectorAggregates:
    def test_count_by_event_name_consolidates_related_counters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(
            monkeypatch,
            {
                "data": {
                    "result": [
                        {"metric": {"event_name": "turn_end"}, "value": [1, "4"]},
                        {"metric": {"event_name": "exec"}, "value": [1, "2"]},
                        {"metric": {"event_name": "exec_failed"}, "value": [1, "1"]},
                    ]
                }
            },
        )
        counts = loki_events.count_by_event_name(
            agent_id=7,
            event_names=["^turn_end$", "^exec$", "^exec_.*"],
            categories=["telemetry", "log"],
            attribute_filters=None,
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 1, 3, tzinfo=UTC),
        )
        assert counts == {"turn_end": 4, "exec": 2, "exec_failed": 1}
        query = client.calls[0][1]["query"]
        assert query.startswith("sum by (event_name) (count_over_time((")
        assert "[10800s]))" in query

    def test_attribute_distribution_relabels_the_bucketed_series_limit_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(
            monkeypatch,
            [
                _SeriesLimitResponse(),
                {
                    "data": {
                        "result": [
                            {
                                "metric": {"duration_seconds_bucket": "2"},
                                "value": [1, "3"],
                            }
                        ]
                    }
                },
            ],
        )
        distribution = loki_events.attribute_distribution(
            field="duration_seconds",
            agent_id=7,
            event_names=["^turn_end$"],
            categories=["telemetry", "log"],
            attribute_filters=None,
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 1, 3, tzinfo=UTC),
        )
        assert distribution == [(2.5, 3)]
        assert len(client.calls) == 2
        assert "topk(500" in client.calls[1][1]["query"]


# ─── attribute filters + attribute_aggregate ─────────────────────────────────


class TestAttributeFilters:
    def test_attribute_filter_stages(self) -> None:
        q = loki_events._build_logql(attribute_filters={"ok": "true", "node": "!=claim"})
        assert '| json ok="attributes.ok" | ok="true"' in q
        assert '| json node="attributes.node" | node!="claim"' in q

    def test_count_events_with_attribute_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch, {"data": {"result": [{"metric": {}, "value": [1723300000, "7"]}]}}
        )
        n = loki_events.count_events(
            event_names=["turn_end"],
            cluster=".ava-preview",
            agent_id=3,
            attribute_filters={"ok": "true"},
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
        )
        assert n == 7
        q = client.calls[0][1]["query"]
        assert '| json ok="attributes.ok" | ok="true"' in q
        assert '| cluster=".ava-preview" or cluster=""' in q
        assert '| event_name_extracted=~"turn_end"' in q


class TestAttributeAggregate:
    def _q(self, client: _FakeClient) -> str:
        return client.calls[0][1]["query"]

    def test_attribute_aggregate_caches_scalar_and_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from_ = datetime(2026, 8, 1, tzinfo=UTC)
        to = datetime(2026, 8, 2, tzinfo=UTC)
        scalar_client = _install(
            monkeypatch,
            {"data": {"result": [{"metric": {}, "value": [1, "42.5"]}]}},
        )

        assert loki_events.attribute_aggregate(
            field="duration_seconds", agg="sum", from_=from_, to=to
        ) == loki_events.attribute_aggregate(
            field="duration_seconds", agg="sum", from_=from_, to=to
        )
        assert len(scalar_client.calls) == 1

        list_client = _install(
            monkeypatch,
            {
                "data": {
                    "result": [
                        {"metric": {"model": "deepseek-v4-flash"}, "value": [1, "100.0"]},
                        {"metric": {"model": "deepseek-v4-pro"}, "value": [1, "200.0"]},
                    ]
                }
            },
        )
        first = loki_events.attribute_aggregate(
            field="in_total", agg="sum", group_by="model", from_=from_, to=to
        )
        first.append(("caller-mutation", -1.0))
        second = loki_events.attribute_aggregate(
            field="in_total", agg="sum", group_by="model", from_=from_, to=to
        )

        assert second == [("deepseek-v4-flash", 100.0), ("deepseek-v4-pro", 200.0)]
        assert len(list_client.calls) == 1

    def test_attribute_aggregate_does_not_cache_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(monkeypatch, [_SeriesLimitResponse(), _SeriesLimitResponse()])

        def aggregate() -> float | list[tuple[str, float]]:
            return loki_events.attribute_aggregate(
                field="duration_seconds",
                agg="sum",
                from_=datetime(2026, 8, 1, tzinfo=UTC),
                to=datetime(2026, 8, 2, tzinfo=UTC),
            )

        with pytest.raises(httpx.HTTPStatusError):
            aggregate()
        with pytest.raises(httpx.HTTPStatusError):
            aggregate()
        assert len(client.calls) == 2

    def test_attribute_aggregate_deduplicates_concurrent_same_key_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = _FakeResponse(
            {
                "data": {
                    "result": [
                        {"metric": {"model": "deepseek-v4-flash"}, "value": [1, "100.0"]},
                        {"metric": {"model": "deepseek-v4-pro"}, "value": [1, "200.0"]},
                    ]
                }
            }
        )
        client = _SlowClient(response)
        monkeypatch.setattr(loki_events, "_client", _accessor(client))
        barrier = threading.Barrier(2)

        def aggregate() -> float | list[tuple[str, float]]:
            barrier.wait(timeout=1)
            return loki_events.attribute_aggregate(
                field="in_total",
                agg="sum",
                group_by="model",
                from_=datetime(2026, 8, 1, tzinfo=UTC),
                to=datetime(2026, 8, 2, tzinfo=UTC),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=2) for future in [pool.submit(aggregate) for _ in range(2)]
            ]

        expected = [("deepseek-v4-flash", 100.0), ("deepseek-v4-pro", 200.0)]
        assert results == [expected, expected]
        assert results[0] is not results[1]
        assert len(client.calls) == 1

    def test_attribute_aggregate_waiter_isolated_from_leader_list_mutation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = _FakeResponse(
            {
                "data": {
                    "result": [
                        {"metric": {"model": "deepseek-v4-flash"}, "value": [1, "100.0"]},
                    ]
                }
            }
        )
        release_leader_query = threading.Event()
        client = _SlowClient(response, release=release_leader_query)
        monkeypatch.setattr(loki_events, "_client", _accessor(client))
        waiter_claimed = threading.Event()
        waiter_has_result = threading.Event()
        release_waiter = threading.Event()
        real_begin = loki_events_cache.begin

        class _GatedEvent(threading.Event):
            def __init__(self, event: threading.Event) -> None:
                super().__init__()
                self._event = event

            def set(self) -> None:
                self._event.set()

            def wait(self, timeout: float | None = None) -> bool:
                ready = self._event.wait(timeout)
                waiter_has_result.set()
                assert release_waiter.wait(timeout=2)
                return ready

        def gate_waiter(key: tuple[object, ...]) -> tuple[loki_events_cache._Inflight, bool]:
            holder, is_leader = real_begin(key)
            if not is_leader:
                holder.event = _GatedEvent(holder.event)
                waiter_claimed.set()
            return holder, is_leader

        monkeypatch.setattr(loki_events_cache, "begin", gate_waiter)

        def aggregate() -> float | list[tuple[str, float]]:
            return loki_events.attribute_aggregate(
                field="in_total",
                agg="sum",
                group_by="model",
                from_=datetime(2026, 8, 1, tzinfo=UTC),
                to=datetime(2026, 8, 2, tzinfo=UTC),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(aggregate)
            assert client.entered.wait(timeout=1)
            waiter = pool.submit(aggregate)
            try:
                assert waiter_claimed.wait(timeout=1)
                release_leader_query.set()
                assert waiter_has_result.wait(timeout=2)
                leader_result = leader.result(timeout=1)
                assert isinstance(leader_result, list)
                leader_result.append(("caller-mutation", -1.0))
            finally:
                release_leader_query.set()
                release_waiter.set()
            waiter_result = waiter.result(timeout=1)

        assert waiter_result == [("deepseek-v4-flash", 100.0)]

    def test_attribute_aggregate_propagates_leader_failure_to_waiter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _SlowClient(_SeriesLimitResponse())
        monkeypatch.setattr(loki_events, "_client", _accessor(client))
        barrier = threading.Barrier(2)

        def aggregate() -> float | list[tuple[str, float]]:
            barrier.wait(timeout=1)
            return loki_events.attribute_aggregate(
                field="duration_seconds",
                agg="sum",
                from_=datetime(2026, 8, 1, tzinfo=UTC),
                to=datetime(2026, 8, 2, tzinfo=UTC),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(aggregate) for _ in range(2)]
            for future in futures:
                with pytest.raises(httpx.HTTPStatusError):
                    future.result(timeout=2)

        assert len(client.calls) == 1

    def test_attribute_aggregate_wait_timeout_falls_back_to_own_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _SlowClient(
            _FakeResponse({"data": {"result": [{"metric": {}, "value": [1, "42.5"]}]}}),
            delay_s=0.1,
        )
        monkeypatch.setattr(loki_events, "_client", _accessor(client))
        monkeypatch.setattr(loki_events_cache, "_INFLIGHT_WAIT_S", 0.01)
        barrier = threading.Barrier(2)

        def aggregate() -> float | list[tuple[str, float]]:
            barrier.wait(timeout=1)
            return loki_events.attribute_aggregate(
                field="duration_seconds",
                agg="sum",
                from_=datetime(2026, 8, 1, tzinfo=UTC),
                to=datetime(2026, 8, 2, tzinfo=UTC),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=2) for future in [pool.submit(aggregate) for _ in range(2)]
            ]

        assert results == [42.5, 42.5]
        assert len(client.calls) == 2

    @pytest.mark.parametrize(
        ("era", "indexed_labeled"),
        [(LokiReadEra.LEGACY, False), (LokiReadEra.INDEXED, True)],
    )
    def test_agent_llm_usage_pipeline_filters_on_body_truth(
        self, era: LokiReadEra, indexed_labeled: bool
    ) -> None:
        q = loki_events._agg_pipeline(
            era=era,
            indexed_labeled=indexed_labeled,
            agent_id=42,
            event_names=["llm_usage"],
        )

        assert '| json agent_id_extracted="agent_id" | agent_id_extracted="42"' in q
        assert ('| json event_name_extracted="event_name" | event_name_extracted=~"llm_usage"') in q
        assert '| agent_id="42"' not in q
        assert '| event_name=~"llm_usage"' not in q

    def test_sum_scalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": [{"metric": {}, "value": [1, "42.5"]}]}})
        v = loki_events.attribute_aggregate(
            field="duration_seconds",
            agg="sum",
            event_names=["turn_end"],
            cluster=".ava-preview",
            agent_id=3,
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
        )
        assert v == 42.5
        q = self._q(client)
        assert q.startswith("sum(sum_over_time((")
        assert 'agent_id_extracted="3"' in q
        assert 'cluster=".ava-preview" or cluster=""' in q
        assert 'event_name_extracted=~"turn_end"' in q
        assert '| json duration_seconds="attributes.duration_seconds"' in q
        assert "| unwrap duration_seconds" in q
        assert "[86400s])" in q
        assert "| json |" not in q  # no plain json stage — would split series
        assert client.calls[0][0].endswith("/loki/api/v1/query")  # instant endpoint

    def test_min_max_wrappers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": [{"metric": {}, "value": [1, "3.1"]}]}})
        assert loki_events.attribute_aggregate(field="d", agg="min") == 3.1
        assert self._q(client).startswith("min(min_over_time((")
        client = _install(monkeypatch, {"data": {"result": [{"metric": {}, "value": [1, "9.9"]}]}})
        assert loki_events.attribute_aggregate(field="d", agg="max") == 9.9
        assert self._q(client).startswith("max(max_over_time((")

    def test_quantile_series_limit_falls_back_to_bucketed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whole-life distributions exceed Loki's max_query_series (500) —
        the exact count-by-value histogram is rejected and the aggregate
        re-fetches it bucketed to integer seconds (cardinality bounded by
        the duration RANGE, not the event count), topk-capped, with bucket
        midpoints as value representatives. Regression: inspector no-hours
        500 (2026-08-13, prod)."""
        # buckets 1.5 x2, 2.5 x3, 10.5 x5 -> p50 over 10 samples: rank 4.5,
        # interpolating between rank 4 (2.5) and rank 5 (10.5) = 6.5
        payload = {
            "data": {
                "result": [
                    {"metric": {"duration_seconds_bucket": "1"}, "value": [1, "2"]},
                    {"metric": {"duration_seconds_bucket": "2"}, "value": [1, "3"]},
                    {"metric": {"duration_seconds_bucket": "10"}, "value": [1, "5"]},
                ]
            }
        }
        client = _install(monkeypatch, [_SeriesLimitResponse(), payload])
        v = loki_events.attribute_aggregate(
            field="duration_seconds",
            agg="quantile",
            quantile=0.5,
            event_names=["turn_end"],
            agent_id=3,
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
        )
        assert abs(v - 6.5) < 1e-9
        assert len(client.calls) == 2  # exact attempt, then bucketed retry
        q1 = client.calls[0][1]["query"]
        q2 = client.calls[1][1]["query"]
        assert "sum by (duration_seconds) (count_over_time" in q1
        assert "topk(500, sum by (duration_seconds_bucket) (count_over_time" in q2
        assert (
            'label_format duration_seconds_bucket="{{ regexReplaceAll \\"([0-9]+)[.][0-9]+\\" .duration_seconds \\"$1\\" }}"'
            in q2
        )
        assert 'event_name_extracted=~"turn_end"' in q2

    def test_quantile_non_series_limit_400_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the max_query_series rejection triggers the bucketed retry —
        any other Loki 400 (or the same code with a different message)
        propagates instead of silently degrading."""
        boom = _SeriesLimitResponse()
        boom.text = "parse error: something else"
        client = _install(monkeypatch, [boom])
        with pytest.raises(httpx.HTTPStatusError):
            loki_events.attribute_aggregate(field="duration_seconds", agg="quantile", quantile=0.5)
        assert len(client.calls) == 1

    def test_quantile_client_side_distribution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # values 1.0 x2, 2.0 x3, 10.0 x5 -> p50 over 10 samples: rank 4.5,
        # interpolating between rank 4 (2.0) and rank 5 (10.0) = 6.0
        # (percentile_cont semantics — the old SQL used percentile_cont too)
        payload = {
            "data": {
                "result": [
                    {"metric": {"duration_seconds": "1.0"}, "value": [1, "2"]},
                    {"metric": {"duration_seconds": "2.0"}, "value": [1, "3"]},
                    {"metric": {"duration_seconds": "10.0"}, "value": [1, "5"]},
                ]
            }
        }
        client = _install(monkeypatch, payload)
        v = loki_events.attribute_aggregate(field="duration_seconds", agg="quantile", quantile=0.5)
        assert abs(v - 6.0) < 1e-9
        q = self._q(client)
        assert "count_over_time" in q
        assert "quantile_over_time" not in q  # computed client-side

    def test_quantile_interpolates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 1.0 x1, 2.0 x1 -> p25: rank 0.25, between 1.0 (rank 0) and 2.0 (rank 1)
        payload = {
            "data": {
                "result": [
                    {"metric": {"d": "1.0"}, "value": [1, "1"]},
                    {"metric": {"d": "2.0"}, "value": [1, "1"]},
                ]
            }
        }
        _install(monkeypatch, payload)
        v = loki_events.attribute_aggregate(field="d", agg="quantile", quantile=0.25)
        assert abs(v - 1.25) < 1e-9

    def test_group_by_sum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "data": {
                "result": [
                    {"metric": {"model": "deepseek-v4-flash"}, "value": [1, "100.0"]},
                    {"metric": {"model": "deepseek-v4-pro"}, "value": [1, "200.0"]},
                ]
            }
        }
        client = _install(monkeypatch, payload)
        out = loki_events.attribute_aggregate(field="in_total", agg="sum", group_by="model")
        assert out == [("deepseek-v4-flash", 100.0), ("deepseek-v4-pro", 200.0)]
        q = self._q(client)
        assert "sum by (model) (sum_over_time((" in q
        assert '| json model="attributes.model"' in q

    def test_group_by_count_and_quantile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})
        assert loki_events.attribute_aggregate(field="x", agg="count", group_by="model") == []
        assert "count_over_time" in self._q(client)
        assert "| json model=" in self._q(client)
        client = _install(monkeypatch, {"data": {"result": []}})
        assert (
            loki_events.attribute_aggregate(
                field="x", agg="quantile", quantile=0.5, group_by="model"
            )
            == []
        )
        assert "sum by (model, x)" in self._q(client)

    def test_empty_window_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})
        assert (
            loki_events.attribute_aggregate(
                field="x",
                agg="sum",
                from_=datetime(2026, 8, 2, tzinfo=UTC),
                to=datetime(2026, 8, 1, tzinfo=UTC),
            )
            == 0.0
        )
        assert client.calls == []

    def test_quantile_requires_quantile_param(self) -> None:
        with pytest.raises(ValueError):
            loki_events.attribute_aggregate(field="x", agg="quantile")

    def test_unknown_agg_rejected(self) -> None:
        with pytest.raises(ValueError):
            loki_events.attribute_aggregate(field="x", agg="avg")


# ─── query_projected_lines ───────────────────────────────────────────────────


class _RepeatingClient:
    """Serves the first payload once (the count pre-query), then the same
    range payload for every slice fetch — bisection issues an unbounded
    number of slice requests."""

    def __init__(self, first: dict[str, Any], repeat: dict[str, Any]) -> None:
        self.first: list[dict[str, Any]] = [first]
        self.repeat = repeat
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
        self.calls.append((url, params))
        return _FakeResponse(self.first.pop(0) if self.first else self.repeat)


class TestQueryProjectedLines:
    def test_timeout_is_threaded_to_count_and_projected_slices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timeouts: list[float | None] = []

        def get_json(
            _url: str,
            _params: dict[str, Any],
            *,
            endpoint: str,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            timeouts.append(timeout_s)
            assert endpoint in {"query", "query_range"}
            return {"data": {"result": []}}

        monkeypatch.setattr(loki_events, "_get_json", get_json)
        loki_events.query_projected_lines(
            fields=[],
            template="{{ __line__ }}",
            event_names=["agent_spawned"],
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
            timeout_s=8.0,
        )
        assert timeouts
        assert set(timeouts) == {8.0}

    def test_returns_projected_rows_ascending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        count_payload: dict[str, Any] = {"data": {"result": [{"metric": {}, "value": [1, "2"]}]}}
        page: dict[str, Any] = {
            "data": {
                "result": [
                    {
                        "stream": {"agent_id": "7"},
                        "values": [
                            ["1723000000000000002", "b"],
                            ["1723000000000000001", "a"],
                        ],
                    }
                ]
            }
        }
        _install(monkeypatch, [count_payload, page])
        rows = loki_events.query_projected_lines(
            fields=["name"],
            template="{{ .name }}",
            event_names=["service_started"],
            from_=datetime.fromtimestamp(1_722_999_999, UTC),
            to=datetime.fromtimestamp(1_723_000_001, UTC),
        )
        assert rows == [
            (1723000000000000001, 7, "a"),
            (1723000000000000002, 7, "b"),
        ]

    def test_bisection_floor_truncation_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A slice still filling its limit at the bisection floor (1s wide)
        must raise instead of silently keeping a truncated slice — silent row
        loss skews the caller's reduction (was: rows dropped with no signal)."""
        count_payload: dict[str, Any] = {"data": {"result": [{"metric": {}, "value": [1, "4"]}]}}
        full_page: dict[str, Any] = {
            "data": {
                "result": [
                    {
                        "stream": {},
                        "values": [
                            ["1723000000500000000", "x"],
                            ["1723000000400000000", "y"],
                        ],
                    }
                ]
            }
        }
        client = _RepeatingClient(count_payload, full_page)
        monkeypatch.setattr(loki_events, "_client", _accessor(client))
        with pytest.raises(RuntimeError, match="bisection floor"):
            loki_events.query_projected_lines(
                fields=["name"],
                template="{{ .name }}",
                event_names=["service_started"],
                from_=datetime.fromtimestamp(1_723_000_000, UTC),
                to=datetime.fromtimestamp(1_723_000_004, UTC),
                limit_per_slice=2,
            )

    @pytest.mark.parametrize("era", [LokiReadEra.LEGACY, LokiReadEra.INDEXED])
    def test_projected_lines_filter_on_body_truth_event_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        era: LokiReadEra,
    ) -> None:
        """The code_len/output_len projections (metrics A3, task #1409) must
        filter on body-truth event_name. Structured-metadata labels are
        batch-reused in the legacy era — streams labeled event_name="code"
        carry mixed log/llm_usage lines whose bodies lack `attributes.body` —
        so an SM-label filter lets non-code lines reach
        `line_format "{{ len .body }}"` and renders 0 (the distributions
        were zero-dominated from the 08-23 cutover until #1515 switched the
        filter to `event_name_extracted`)."""
        if era is LokiReadEra.INDEXED:

            def _indexed_slices(
                window: tuple[datetime, datetime],
            ) -> tuple[LokiReadSlice, ...]:
                return (LokiReadSlice(LokiReadEra.INDEXED, *window),)

            monkeypatch.setattr(loki_events, "_read_slices", _indexed_slices)
        client = _install(monkeypatch, {"data": {"result": []}})
        loki_events.query_projected_lines(
            fields=["body"],
            template="{{ len .body }}",
            event_names=["code"],
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
        )
        range_queries = [
            params["query"] for _url, params in client.calls if _url.endswith("/query_range")
        ]
        assert range_queries
        for q in range_queries:
            assert '| json event_name_extracted="event_name" | event_name_extracted=~"code"' in q
            assert '| event_name=~"code"' not in q
        if era is LokiReadEra.INDEXED:
            assert 'event_name="code"' in range_queries[0]
        else:
            # The legacy selector must stay matcher-free — an SM event_name
            # matcher there would reintroduce the batch-reuse label mismatch.
            assert 'event_name="code"' not in range_queries[0]


# ─── count_events_series / attribute_max_series (ops panel, task #1197) ─────


class TestCountEventsSeries:
    def test_range_query_shape_and_grouping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch,
            {
                "data": {
                    "result": [
                        {"metric": {"kind": "queue_full"}, "values": [[1723300200, "4"]]},
                        {"metric": {"kind": "publish_error"}, "values": [[1723300200, "7"]]},
                    ]
                }
            },
        )
        out = loki_events.count_events_series(
            event_names=["sse_drop"],
            cluster=".ava-preview",
            group_by="kind",
            from_attributes=True,
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
            step_s=300,
        )
        assert out == {"queue_full": [(1723300200, 4)], "publish_error": [(1723300200, 7)]}
        _url, params = client.calls[0]
        assert _url.endswith("/loki/api/v1/query_range")
        q = params["query"]
        assert q.startswith("sum by (kind) (count_over_time((")
        assert '| event_name_extracted=~"sse_drop"' in q
        assert '| cluster=".ava-preview" or cluster=""' in q
        assert '| json kind="attributes.kind"' in q
        assert q.endswith(")[300s]))")
        assert params["step"] == "300s"
        assert params["start"] == datetime(2026, 8, 1, tzinfo=UTC).timestamp()
        assert params["end"] == datetime(2026, 8, 2, tzinfo=UTC).timestamp()

    def test_ungrouped_returns_single_empty_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch,
            {"data": {"result": [{"metric": {}, "values": [[1723300200, "3"]]}]}},
        )
        out = loki_events.count_events_series(
            event_names=["agent_restarted"],
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
            step_s=60,
        )
        assert out == {"": [(1723300200, 3)]}
        q = client.calls[0][1]["query"]
        assert q.startswith("sum(count_over_time((")
        assert "sum by" not in q

    def test_straddle_keeps_the_grid_and_adds_each_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(
            monkeypatch,
            [
                {"data": {"result": [{"metric": {}, "values": [[1786311000, "2"]]}]}},
                {"data": {"result": [{"metric": {}, "values": [[1786311000, "3"]]}]}},
            ],
        )

        def _slices(_window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
            return _straddled_slices()

        monkeypatch.setattr(loki_events, "_read_slices", _slices)

        out = loki_events.count_events_series(
            event_names=["sse_drop"],
            from_=_ROLL_OUT_START,
            to=_ROLL_OUT_END,
            step_s=300,
        )

        assert out == {"": [(1786311000, 5)]}
        assert [call[1]["start"] for call in client.calls] == [_ROLL_OUT_START.timestamp()] * 2
        assert [call[1]["end"] for call in client.calls] == [_ROLL_OUT_END.timestamp()] * 2
        assert 'event_name=""' in client.calls[0][1]["query"]
        assert 'event_name!="", event_name="sse_drop"' in client.calls[1][1]["query"]

    def test_empty_window_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})
        assert (
            loki_events.count_events_series(
                event_names=["x"],
                from_=datetime(2026, 8, 2, tzinfo=UTC),
                to=datetime(2026, 8, 1, tzinfo=UTC),
                step_s=60,
            )
            == {}
        )
        assert client.calls == []


class TestAttributeMaxSeries:
    def test_unwrap_max_range_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch,
            {"data": {"result": [{"metric": {}, "values": [[1723300200, "4000.5"]]}]}},
        )
        out = loki_events.attribute_max_series(
            field="latency_ms",
            event_names=["llm_usage"],
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
            step_s=300,
        )
        assert out == [(1723300200, 4000.5)]
        _url, params = client.calls[0]
        assert _url.endswith("/loki/api/v1/query_range")
        q = params["query"]
        assert q.startswith("max(max_over_time((")
        assert '| json latency_ms="attributes.latency_ms"' in q
        assert "| unwrap latency_ms" in q
        assert q.endswith(")[300s]))")
        assert params["step"] == "300s"

    def test_empty_window_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})
        assert (
            loki_events.attribute_max_series(
                field="latency_ms",
                event_names=["llm_usage"],
                from_=datetime(2026, 8, 2, tzinfo=UTC),
                to=datetime(2026, 8, 1, tzinfo=UTC),
                step_s=60,
            )
            == []
        )
        assert client.calls == []

    def test_straddle_uses_the_bucket_maximum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            [
                {"data": {"result": [{"metric": {}, "values": [[1786311000, "2.5"]]}]}},
                {"data": {"result": [{"metric": {}, "values": [[1786311000, "3.5"]]}]}},
            ],
        )

        def _slices(_window: tuple[datetime, datetime]) -> tuple[LokiReadSlice, ...]:
            return _straddled_slices()

        monkeypatch.setattr(loki_events, "_read_slices", _slices)

        assert loki_events.attribute_max_series(
            field="latency_ms",
            event_names=["llm_usage"],
            from_=_ROLL_OUT_START,
            to=_ROLL_OUT_END,
            step_s=300,
        ) == [(1786311000, 3.5)]


class _RaisingClient:
    """Stands in for the shared client; get() raises the configured error."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
        self.calls.append((url, params))
        raise self.exc


class _StatusClient:
    """Stands in for the shared client; returns an httpx response with the
    configured status so raise_for_status raises the real HTTPStatusError."""

    def __init__(self, status: int) -> None:
        self._resp = httpx.Response(status, request=httpx.Request("GET", "http://loki"))

    def get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        return self._resp


class TestGetJson:
    """The shared fetch helper: per-call failure events (task #1289 — a
    60s Loki hang surfaced as a bare /api/events 500 with no record of the
    query shape; `loki_query_failed` is that record)."""

    def test_timeout_emits_failure_event_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _RaisingClient(httpx.ReadTimeout("timed out"))
        monkeypatch.setattr(loki_events, "_client", _accessor(client))
        emitted: list[tuple[str, str, dict[str, Any]]] = []

        def _emit(category: str, name: str, **kwargs: Any) -> None:
            emitted.append((category, name, kwargs))

        monkeypatch.setattr(loki_events.telemetry, "emit", _emit)
        params: dict[str, Any] = {
            "query": '{service_name="unknown_service"} | json | category=~"telemetry"',
            "limit": 1001,
            "start": 1787068800000000000,
            "end": 1787155200000000000,
        }
        with pytest.raises(httpx.ReadTimeout):
            loki_events._get_json(
                "http://loki/loki/api/v1/query_range",
                params,
                endpoint="query_range",
            )
        assert len(emitted) == 1
        category, name, kwargs = emitted[0]
        assert category == "log"
        assert name == "loki_query_failed"
        attrs = kwargs["attributes"]
        assert kwargs["level"] == "error"
        assert attrs["endpoint"] == "query_range"
        assert attrs["error"] == "ReadTimeout"
        assert attrs["window_from"] == "2026-08-18T16:00:00+00:00"
        assert attrs["window_to"] == "2026-08-19T16:00:00+00:00"
        assert attrs["query"].startswith("{service_name=")

    def test_http_5xx_emits_failure_event_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _StatusClient(500)
        monkeypatch.setattr(loki_events, "_client", _accessor(client))
        emitted: list[tuple[str, str, dict[str, Any]]] = []

        def _emit(category: str, name: str, **kwargs: Any) -> None:
            emitted.append((category, name, kwargs))

        monkeypatch.setattr(loki_events.telemetry, "emit", _emit)
        with pytest.raises(httpx.HTTPStatusError):
            loki_events._get_json(
                "http://loki/loki/api/v1/query",
                {"query": "sum(count_over_time(({}[1d])))"},
                endpoint="query",
            )
        assert len(emitted) == 1
        assert emitted[0][1] == "loki_query_failed"
        assert emitted[0][2]["attributes"]["error"] == "HTTPStatusError"

    def test_emit_failure_does_not_mask_transport_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _RaisingClient(httpx.ReadTimeout("timed out"))
        monkeypatch.setattr(loki_events, "_client", _accessor(client))

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise ValueError("unregistered event")

        monkeypatch.setattr(loki_events.telemetry, "emit", _boom)
        with pytest.raises(httpx.ReadTimeout):
            loki_events._get_json(
                "http://loki/loki/api/v1/query_range",
                {"query": '{service_name=~".+"}'},
                endpoint="query_range",
            )

    def test_success_returns_payload_without_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})
        emitted: list[tuple[str, str, dict[str, Any]]] = []

        def _emit(category: str, name: str, **kwargs: Any) -> None:
            emitted.append((category, name, kwargs))

        monkeypatch.setattr(loki_events.telemetry, "emit", _emit)
        payload = loki_events._get_json(
            "http://loki/loki/api/v1/query",
            {"query": "sum(count_over_time(({}[1d])))"},
            endpoint="query",
        )
        assert payload == {"data": {"result": []}}
        assert emitted == []
        assert client.calls == [
            ("http://loki/loki/api/v1/query", {"query": "sum(count_over_time(({}[1d])))"})
        ]
