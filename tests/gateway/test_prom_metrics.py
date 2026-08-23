"""Unit tests for `gateway/prom_metrics.py` — the Prometheus read side of the
LGTM cutover (task #1197).

The module's only I/O is httpx GETs through the shared client accessor
(`prom_metrics._client`); these tests swap the accessor for a fake and
assert on the query text, the URL, and the result parsing (labels + float
values, missing-value rows skipped, empty result []).
"""

from __future__ import annotations

import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from gateway import prom_metrics

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
            request = httpx.Request("GET", "http://prometheus.invalid")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "prometheus response error", request=request, response=response
            )


class _FakeClient:
    """Records the request, returns canned payloads; stands in for the shared
    client behind `prom_metrics._client()`."""

    def __init__(self, payloads: list[dict[str, Any]] | dict[str, Any], status: int = 200) -> None:
        self.payloads = payloads if isinstance(payloads, list) else [payloads]
        self.status = status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
        self.calls.append((url, params))
        return _FakeResponse(self.payloads.pop(0), status=self.status)


def _accessor(client: object) -> Any:
    """`prom_metrics._client` replacement: hands back the fake."""

    def _get() -> Any:
        return client

    return _get


def _install(
    monkeypatch: pytest.MonkeyPatch, payloads: list[dict[str, Any]] | dict[str, Any]
) -> _FakeClient:
    client = _FakeClient(payloads)
    monkeypatch.setattr(prom_metrics, "_client", _accessor(client))
    return client


def _prom_payload(series: list[tuple[dict[str, str], str]]) -> dict[str, Any]:
    """One instant-query payload: `result` items with metric labels + a
    `[ts, "value"]` pair, the shape Prometheus returns."""
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": labels, "value": [1786566000.0, value]} for labels, value in series
            ]
        },
    }


@pytest.fixture(autouse=True)
def _fresh_query_budget() -> Generator[None, None, None]:
    prom_metrics.reset_for_tests()
    yield
    prom_metrics.reset_for_tests()


# ─── query(): URL + parse semantics ──────────────────────────────────────────


class TestQuery:
    def test_hits_instant_query_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _prom_payload([]))
        prom_metrics.query("up")
        url, params = client.calls[0]
        assert url == "http://127.0.0.1:9090/api/v1/query"
        assert params == {"query": "up"}

    def test_returns_labels_and_float_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _prom_payload([({"agent_id": "7", "model": "deepseek-v4-flash"}, "1234.5")]),
        )
        rows = prom_metrics.query("sum by (agent_id) (ava_llm_usage_in_total)")
        assert rows == [({"agent_id": "7", "model": "deepseek-v4-flash"}, 1234.5)]

    def test_empty_result_is_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _prom_payload([]))
        assert prom_metrics.query("ava_llm_usage_in_total") == []

    def test_missing_result_key_is_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, {"status": "success", "data": {}})
        assert prom_metrics.query("up") == []

    def test_skips_rows_without_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient({"status": "success", "data": {"result": [{"metric": {}}]}})
        monkeypatch.setattr(prom_metrics, "_client", _accessor(client))
        assert prom_metrics.query("up") == []

    def test_non_2xx_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient({"status": "error"}, status=422)
        monkeypatch.setattr(prom_metrics, "_client", _accessor(client))
        with pytest.raises(httpx.HTTPStatusError):
            prom_metrics.query("up")

    def test_transport_failure_emits_query_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FailedClient:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                raise httpx.ReadTimeout("prometheus timed out")

        failures: list[dict[str, object]] = []

        def _record(**kwargs: object) -> None:
            failures.append(kwargs)

        monkeypatch.setattr(prom_metrics, "_client", _accessor(_FailedClient()))
        monkeypatch.setattr(prom_metrics, "_log_prom_failure", _record)

        with pytest.raises(httpx.ReadTimeout):
            prom_metrics.query("sum(up)")

        assert len(failures) == 1
        failure = failures[0]
        assert failure["endpoint"] == "query"
        assert isinstance(failure["duration_s"], float)
        assert failure["error"] == "ReadTimeout"
        assert failure["query"] == "sum(up)"

    def test_transport_failure_emits_first_and_each_fiftieth_after_success_reset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sustained Prometheus outage stays visible without flooding events."""

        class _FailedClient:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                raise httpx.ReadTimeout("prometheus timed out")

        emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def _emit(*args: object, **kwargs: object) -> None:
            emitted.append((args, kwargs))

        monkeypatch.setattr(prom_metrics.telemetry, "emit", _emit)
        monkeypatch.setattr(prom_metrics, "_client", _accessor(_FailedClient()))

        for _ in range(50):
            with pytest.raises(httpx.ReadTimeout):
                prom_metrics.query("sum(up)")

        monkeypatch.setattr(prom_metrics, "_client", _accessor(_FakeClient(_prom_payload([]))))
        assert prom_metrics.query("sum(up)") == []

        monkeypatch.setattr(prom_metrics, "_client", _accessor(_FailedClient()))
        for _ in range(50):
            with pytest.raises(httpx.ReadTimeout):
                prom_metrics.query("sum(up)")

        assert len(emitted) == 4
        for args, kwargs in emitted:
            assert args == ("log", "prom_query_failed")
            assert kwargs["level"] == "error"
            attributes = kwargs["attributes"]
            assert isinstance(attributes, dict)
            assert attributes["endpoint"] == "query"
            assert attributes["error"] == "ReadTimeout"
            assert attributes["query"] == "sum(up)"


class TestQueryBudget:
    def test_query_holds_a_prometheus_budget_slot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        entered = threading.Event()
        release = threading.Event()

        class _BlockingClient:
            def get(self, url: str, params: dict[str, Any]) -> _FakeResponse:
                entered.set()
                assert release.wait(timeout=1)
                return _FakeResponse(_prom_payload([]))

        monkeypatch.setattr(prom_metrics, "_client", _accessor(_BlockingClient()))
        prom_metrics.reset_for_tests(capacity=1)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(prom_metrics.query, "up")
            assert entered.wait(timeout=1)
            assert prom_metrics.prom_query_budget.metrics().active == 1
            release.set()
            assert future.result(timeout=1) == []

    def test_queue_full_raises_prometheus_budget_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _install(monkeypatch, _prom_payload([]))
        prom_metrics.reset_for_tests(capacity=1, max_waiters=0)

        with (
            prom_metrics.prom_query_budget.slot(),
            pytest.raises(prom_metrics.PromQueryBudgetError) as excinfo,
        ):
            prom_metrics.query("up")

        assert excinfo.value.reason == "queue_full"
        assert client.calls == []

    def test_acquire_timeout_raises_prometheus_budget_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _prom_payload([]))
        prom_metrics.reset_for_tests(capacity=1, max_waiters=1, wait_timeout_s=0.01)
        entered = threading.Event()
        release = threading.Event()

        def _hold_slot() -> None:
            with prom_metrics.prom_query_budget.slot():
                entered.set()
                assert release.wait(timeout=1)

        with ThreadPoolExecutor(max_workers=1) as executor:
            holder = executor.submit(_hold_slot)
            assert entered.wait(timeout=1)
            with pytest.raises(prom_metrics.PromQueryBudgetError) as excinfo:
                prom_metrics.query("up")
            release.set()
            holder.result(timeout=1)

        assert excinfo.value.reason == "acquire_timeout"


# ─── sum_by(): PromQL shape + grouping ───────────────────────────────────────


class TestSumBy:
    def test_per_call_timeout_reaches_http_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        timeouts: list[float] = []

        class _TimedClient:
            def get(self, url: str, params: dict[str, Any], *, timeout: float) -> _FakeResponse:
                timeouts.append(timeout)
                return _FakeResponse(_prom_payload([]))

        monkeypatch.setattr(prom_metrics, "_client", _accessor(_TimedClient()))

        prom_metrics.sum_by("ava_llm_usage_in_total", "agent_id", timeout_s=8.0)

        assert timeouts == [8.0]

    def test_all_time_query_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(
            monkeypatch,
            _prom_payload([({"agent_id": "7"}, "100"), ({"agent_id": "9"}, "50")]),
        )
        out = prom_metrics.sum_by("ava_llm_usage_in_total", "agent_id")
        assert out == {"7": 100.0, "9": 50.0}
        _url, params = client.calls[0]
        assert params["query"] == "sum by (agent_id) (ava_llm_usage_in_total)"

    def test_windowed_query_text_uses_increase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _prom_payload([]))
        prom_metrics.sum_by("ava_llm_usage_in_total", "model", window=timedelta(hours=24))
        _url, params = client.calls[0]
        assert params["query"] == "sum by (model) (increase(ava_llm_usage_in_total[24h]))"

    def test_five_minute_window_uses_minutes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _prom_payload([]))
        prom_metrics.sum_by("ava_llm_usage_in_total", "model", window=timedelta(minutes=5))
        _url, params = client.calls[0]
        assert params["query"] == "sum by (model) (increase(ava_llm_usage_in_total[5m]))"

    def test_missing_label_groups_under_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A series without the grouping label (e.g. pre-model-tracking events)
        # must aggregate under "" — the same contract as the old SQL's NULL group.
        _install(monkeypatch, _prom_payload([({}, "10"), ({"model": "deepseek-v4-pro"}, "20")]))
        out = prom_metrics.sum_by("ava_llm_usage_in_total", "model")
        assert out == {"": 10.0, "deepseek-v4-pro": 20.0}

    def test_duplicate_label_groups_sum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _prom_payload(
                [
                    ({"agent_id": "7", "process": "a"}, "1"),
                    ({"agent_id": "7", "process": "b"}, "2"),
                ]
            ),
        )
        out = prom_metrics.sum_by("ava_llm_usage_in_total", "agent_id")
        assert out == {"7": 3.0}


# ─── query_range(): URL + parse semantics (ops panel, task #1197) ────────────


class TestQueryRange:
    def _range_payload(
        self, series: list[tuple[dict[str, str], list[tuple[float, str]]]]
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": labels, "values": values} for labels, values in series],
            },
        }

    def test_hits_range_endpoint_with_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, self._range_payload([]))
        prom_metrics.query_range(
            "up",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            step_s=300,
        )
        url, params = client.calls[0]
        assert url == "http://127.0.0.1:9090/api/v1/query_range"
        assert params["query"] == "up"
        assert params["step"] == "300s"
        assert params["start"] == datetime(2026, 8, 1, tzinfo=UTC).timestamp()
        assert params["end"] == datetime(2026, 8, 2, tzinfo=UTC).timestamp()

    def test_parses_values_and_drops_nan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            self._range_payload(
                [
                    (
                        {"le": "10"},
                        [(1723300200.0, "3.5"), (1723300500.0, "NaN"), (1723300800.0, "7")],
                    )
                ]
            ),
        )
        out = prom_metrics.query_range(
            "histogram_quantile(0.5, ...)",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
            step_s=300,
        )
        assert out == [({"le": "10"}, [(1723300200, 3.5), (1723300800, 7.0)])]

    def test_empty_result_and_sparse_series(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, self._range_payload([]))
        assert (
            prom_metrics.query_range(
                "up",
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 2, tzinfo=UTC),
                step_s=60,
            )
            == []
        )
        # a series whose only value is NaN yields no rows at all
        _install(monkeypatch, self._range_payload([({}, [(1723300200.0, "NaN")])]))
        assert (
            prom_metrics.query_range(
                "up",
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 2, tzinfo=UTC),
                step_s=60,
            )
            == []
        )
