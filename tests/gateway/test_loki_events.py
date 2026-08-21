"""Unit tests for `gateway/loki_events.py` — the Loki read side of the
unified event stream (task #1197, LGTM cutover).

The module's only I/O is httpx GETs through the shared client accessor
(`loki_events._client`); these tests swap the accessor for a fake and assert
on the LogQL text, the query params, and the parse/paging semantics
(newest-first merge across streams, in-memory offset paging, +1 lookahead
`has_more`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from gateway import loki_events

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
        item = self.payloads.pop(0)
        return item if isinstance(item, _FakeResponse) else _FakeResponse(item)


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


# ─── _build_logql ────────────────────────────────────────────────────────────


class TestBuildLogql:
    def test_default_is_selector_plus_json(self) -> None:
        assert loki_events._build_logql() == '{service_name="unknown_service"} | json'

    def test_agent_id_filter(self) -> None:
        q = loki_events._build_logql(agent_id=42)
        assert '| agent_id="42"' in q
        assert "| json" in q

    def test_service_only_matches_null_agent_id(self) -> None:
        # json turns a JSON null into an absent field; empty-string matches it.
        assert '| agent_id=""' in loki_events._build_logql(service_only=True)

    def test_grep_is_a_line_filter_before_json(self) -> None:
        q = loki_events._build_logql(grep="boom")
        assert q.startswith('{service_name="unknown_service"} |= "boom" | json')

    def test_categories_become_or_regex(self) -> None:
        q = loki_events._build_logql(categories=["telemetry", "log"])
        assert '| category=~"telemetry|log"' in q

    def test_event_names_become_or_regex(self) -> None:
        q = loki_events._build_logql(event_names=["spawn", "terminate"])
        assert '| event_name=~"spawn|terminate"' in q

    def test_level_min_is_a_threshold_regex(self) -> None:
        q = loki_events._build_logql(level_min="warning")
        assert '| level=~"warning|error|critical"' in q

    def test_level_exact(self) -> None:
        q = loki_events._build_logql(level="warning")
        assert '| level="warning"' in q
        assert "=~" not in q

    def test_machine_and_trace_id(self) -> None:
        q = loki_events._build_logql(machine="machine-1", trace_id="ABCD")
        assert '| machine="machine-1"' in q
        assert '| trace_id="abcd"' in q  # lowercased

    def test_escapes_quotes_and_backslashes(self) -> None:
        q = loki_events._build_logql(grep='say "hi" \\n')
        assert '|= "say \\"hi\\" \\\\n"' in q
        q2 = loki_events._build_logql(grep="line1\nline2")
        assert '|= "line1 line2"' in q2


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
    def test_request_params_and_default_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, _loki_payload([]))
        before = datetime.now(UTC)
        rows, has_more = loki_events.query_events(agent_id=3, limit=100, offset=0)
        after = datetime.now(UTC)
        assert rows == []
        assert has_more is False
        url, params = client.calls[0]
        assert url.endswith("/loki/api/v1/query_range")
        assert params["query"] == '{service_name="unknown_service"} | json | agent_id="3"'
        assert params["direction"] == "backward"
        assert params["limit"] == 101  # limit + offset + 1 lookahead
        # default from_ = now - 24h, to = now (ns)
        start = datetime.fromtimestamp(params["start"] / 1e9, UTC)
        end = datetime.fromtimestamp(params["end"] / 1e9, UTC)
        assert before - timedelta(hours=25) <= start <= after
        assert before - timedelta(seconds=5) <= end <= after + timedelta(seconds=5)
        assert start <= end

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
            machine="machine-1",
            trace_id="abc",
            limit=50,
        )
        q = client.calls[0][1]["query"]
        assert '| agent_id="5"' in q
        assert '| category=~"telemetry|log"' in q
        assert '| event_name=~"spawn|terminate"' in q
        assert '| level=~"warning|error|critical"' in q
        assert '|= "boom"' in q
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
        assert '| agent_id="7"' in q
        assert '| category=~"telemetry|log"' in q
        assert '| event_name=~"spawn"' in q
        assert params["time"] == datetime(2026, 8, 2, tzinfo=UTC).timestamp()

    def test_default_window_is_24h(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": []}})
        assert loki_events.count_events() == 0
        _url, params = client.calls[0]
        assert params["query"].endswith(")[86400s]))")

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
            agent_id=3,
            attribute_filters={"ok": "true"},
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
        )
        assert n == 7
        q = client.calls[0][1]["query"]
        assert '| json ok="attributes.ok" | ok="true"' in q
        assert '| event_name=~"turn_end"' in q


class TestAttributeAggregate:
    def _q(self, client: _FakeClient) -> str:
        return client.calls[0][1]["query"]

    def test_sum_scalar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _install(monkeypatch, {"data": {"result": [{"metric": {}, "value": [1, "42.5"]}]}})
        v = loki_events.attribute_aggregate(
            field="duration_seconds",
            agg="sum",
            event_names=["turn_end"],
            agent_id=3,
            from_=datetime(2026, 8, 1, tzinfo=UTC),
            to=datetime(2026, 8, 2, tzinfo=UTC),
        )
        assert v == 42.5
        q = self._q(client)
        assert q.startswith("sum(sum_over_time((")
        assert 'agent_id="3"' in q
        assert 'event_name=~"turn_end"' in q
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
        assert 'event_name=~"turn_end"' in q2

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
        assert '| event_name=~"sse_drop"' in q
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
