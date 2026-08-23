"""Unit tests for `gateway/prom_metrics.py` — the Prometheus read side of the
LGTM cutover (task #1197).

The module's only I/O is httpx GETs through the shared client accessor
(`prom_metrics._client`); these tests swap the accessor for a fake and
assert on the query text, the URL, and the result parsing (labels + float
values, missing-value rows skipped, empty result []).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
            raise RuntimeError(f"prometheus {self.status_code}")


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
        with pytest.raises(RuntimeError):
            prom_metrics.query("up")


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
        prom_metrics.sum_by("ava_llm_usage_in_total", "model", window_hours=24)
        _url, params = client.calls[0]
        assert params["query"] == "sum by (model) (increase(ava_llm_usage_in_total[24h]))"

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
