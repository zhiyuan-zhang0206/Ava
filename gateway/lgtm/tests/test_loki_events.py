"""Unit tests for `gateway/lgtm/loki_events.py` — the Loki read side of the
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from base.telemetry.loki_index_labels import (
    EVENT_STREAM_RETENTION,
    LOKI_MAX_QUERY_SERIES,
    LOKI_QUERY_CONCURRENCY,
    WAL_DISK_FULL_THRESHOLD,
    LokiReadEra,
    event_stream_selector,
    retention_hours,
    split_index_label_window,
    validate_loki_deploy_config,
)
from gateway.lgtm import _loki_logql, loki_events, loki_query_budget


def test_retention_hours_drives_the_gateway_window_clamp() -> None:
    expected = int(EVENT_STREAM_RETENTION.total_seconds() // 3600)

    assert retention_hours() == expected
    assert min(expected * 2, retention_hours()) == expected
    assert min(expected - 1, retention_hours()) == expected - 1


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
            # Some aggregate paths issue multiple instant queries. Loki
            # returns an empty result for an empty window, so the fake does
            # the same when canned responses are exhausted.
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
_ROLL_OUT_END = _ROLL_OUT_START + timedelta(hours=2)


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
    def test_budget_contract_is_reexported_from_base(self) -> None:
        spec = importlib.util.find_spec("base.telemetry.loki_query_budget")
        assert spec is not None, (
            "base.telemetry.loki_query_budget must own the reusable budget contract"
        )
        base_budget = importlib.import_module("base.telemetry.loki_query_budget")
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
            assert getattr(loki_query_budget, name) is getattr(base_budget, name)

    def test_matches_loki_real_max_concurrent(self) -> None:
        repo = Path(__file__).parents[3]
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

    def test_loki_configs_keep_only_the_archive_retention_override(self) -> None:
        """Audit permanence lives in Postgres, so no event-name retention rule remains."""
        repo = Path(__file__).parents[3]
        for path in ("deploy/lgtm/config/loki.yaml", "deploy/lgtm/native/config/loki.yaml"):
            limits = yaml.safe_load((repo / path).read_text())["limits_config"]
            assert [r["selector"] for r in limits["retention_stream"]] == ['{stream="archive"}']
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
                    "querier": {"max_concurrent": LOKI_QUERY_CONCURRENCY},
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
                    "querier": {"max_concurrent": LOKI_QUERY_CONCURRENCY},
                    "ingester": {"wal": {"disk_full_threshold": 0.95}},
                }
            )

    @pytest.mark.parametrize("variant", ("config", "native/config"))
    def test_loki_demotes_instance_id_and_indexes_event_dimensions(self, variant: str) -> None:
        config_path = Path(__file__).parents[3] / f"deploy/lgtm/{variant}/loki.yaml"
        config = yaml.safe_load(config_path.read_text())
        labels = config["distributor"]["otlp_config"]["default_resource_attributes_as_index_labels"]
        assert labels == [
            "service.name",
            "service.namespace",
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
        monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({"gateway"}))
        monkeypatch.setattr("base.paths.ava_home", lambda: home)
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
        monkeypatch.setattr("base.paths.ava_home", lambda: home)
        monkeypatch.delitem(os.environ, "AVA_TELEMETRY_LOKI_URL", raising=False)
        if override == "marker":
            (home / "lgtm-host").touch()
        elif override == "environment":
            monkeypatch.setitem(os.environ, "AVA_TELEMETRY_LOKI_URL", "http://loki.invalid:3100")
        role = "agent-runner" if override == "runner" else "gateway"
        monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({role}))

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

        loki_events.query_events(from_=_ROLL_OUT_START, to=_ROLL_OUT_END, timeout_s=8.0)

        assert timeouts == [8.0]

    def test_request_params_and_single_indexed_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(monkeypatch, _loki_payload([]))
        assert [
            (s.era, s.start, s.end)
            for s in split_index_label_window(_ROLL_OUT_START, _ROLL_OUT_END)
        ] == [(LokiReadEra.INDEXED, _ROLL_OUT_START, _ROLL_OUT_END)]
        rows, has_more = loki_events.query_events(
            agent_id=3, limit=100, offset=0, from_=_ROLL_OUT_START, to=_ROLL_OUT_END
        )
        assert rows == []
        assert has_more is False
        url, params = client.calls[0]
        assert url.endswith("/loki/api/v1/query_range")
        assert (
            params["query"]
            == '{service_name="unknown_service", stream!="archive", agent_id="3"} | json '
            '| agent_id_extracted="3"'
        )
        assert params["direction"] == "backward"
        assert params["limit"] == 101  # limit + offset + 1 lookahead
        assert len(client.calls) == 1
        assert params["start"] == int(_ROLL_OUT_START.timestamp() * 1e9)
        assert params["end"] == int(_ROLL_OUT_END.timestamp() * 1e9)

    def test_tier_filter_drops_json_parse_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _loki_payload([]))

        loki_events.query_events(tiers=["noise"])

        assert '| __error__=""' in client.calls[0][1]["query"]

    def test_explicit_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _loki_payload([]))
        from_ = datetime(2026, 8, 1, tzinfo=UTC)
        to = datetime(2026, 8, 2, tzinfo=UTC)
        loki_events.query_events(from_=from_, to=to)
        _, params = client.calls[0]
        assert params["start"] == int(from_.timestamp() * 1e9)
        assert params["end"] == int(to.timestamp() * 1e9)

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
