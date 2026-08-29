"""Window-scoped inbound collection for the self-evolution daily scan.

Regression lock for 2026-08-13: ``_inbounds_by_agent`` fetched every inbound
message an agent ever received (no time filter), so ``corrections`` /
``peer_feedback`` accumulated lifetime history into each day's record — the
whole fleet was flagged fumbled every day (1336 "peer feedback" messages
across 70 runs on a day when agents were fine). It now fetches the collection
window plus each agent's first-ever chat message (the task prompt, which may
predate the window for long-running agents).
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from langchain_core.messages import HumanMessage

REF_DIR = (
    Path(__file__).resolve().parents[2]
    / "ava_builtins"
    / "skills"
    / "ava-self-evolution"
    / "reference"
)


@pytest.fixture(scope="module")
def collect_mod() -> Any:
    """Load the reference script as a module — it is a script, not a package
    (the skill directory name has a hyphen), so import via spec + sys.path."""
    sys.path.insert(0, str(REF_DIR))
    try:
        spec = importlib.util.spec_from_file_location("self_ev_collect", REF_DIR / "collect.py")
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["self_ev_collect"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


def _insert_agent(cur: psycopg.Cursor, agent_id: int) -> None:
    cur.execute(
        "INSERT INTO agents (id, label) VALUES (%s, %s)",
        (agent_id, f"test-agent-{agent_id}"),
    )


def _insert_inbound(
    cur: psycopg.Cursor, agent_id: int, source: str, content: str, age: str
) -> None:
    cur.execute(
        "INSERT INTO inbound_messages (agent_id, source, content, kind, created_at) "
        "VALUES (%s, %s, %s, 'chat', now() - %s::interval)",
        (agent_id, source, content, age),
    )


def _fetch(collect_mod: Any, cur: psycopg.Cursor, agent_ids: list[int]) -> Any:
    return collect_mod._inbounds_by_agent(cur, agent_ids, "1 days")


def test_old_history_collapses_to_task_prompt(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    """Only the first-ever message survives from outside the window — not the
    whole lifetime of corrections / peer pings."""
    with db_conn.cursor() as cur:
        _insert_agent(cur, 1)
        _insert_agent(cur, 2)
        _insert_inbound(cur, 1, "user", "task prompt", "10 days")
        _insert_inbound(cur, 1, "user", "old correction \u9519\u4e86", "8 days")
        _insert_inbound(cur, 1, "user", "window follow-up", "1 hour")
        _insert_inbound(cur, 2, "user", "task prompt 2", "30 days")
        _insert_inbound(cur, 2, "agent:99", "old peer ping \u522b\u5fd8\u4e86", "29 days")
    db_conn.commit()

    with db_conn.cursor() as cur:
        out = _fetch(collect_mod, cur, [1, 2])

    assert [m["content"] for m in out[1]] == ["task prompt", "window follow-up"]
    assert [m["content"] for m in out[2]] == ["task prompt 2"]


def test_first_ever_inside_window_not_duplicated(
    collect_mod: Any, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        _insert_agent(cur, 3)
        _insert_inbound(cur, 3, "user", "spawn prompt", "2 hours")
        _insert_inbound(cur, 3, "user", "follow-up", "1 hour")
    db_conn.commit()

    with db_conn.cursor() as cur:
        out = _fetch(collect_mod, cur, [3])

    assert [m["content"] for m in out[3]] == ["spawn prompt", "follow-up"]


def test_agent_without_inbounds_absent(collect_mod: Any, db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        _insert_agent(cur, 4)
    db_conn.commit()

    with db_conn.cursor() as cur:
        out = _fetch(collect_mod, cur, [4])

    assert out.get(4, []) == []


def test_transcript_uses_full_checkpoint_loader(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, object] = {}

    def _load(agent_id: int) -> list[HumanMessage]:
        called["agent_id"] = agent_id
        return [HumanMessage(content="complete history")]

    record_mod = sys.modules[collect_mod._transcript.__module__]
    monkeypatch.setattr(record_mod, "load_checkpoint_messages_full", _load)

    assert collect_mod._transcript(42) == [{"type": "human", "content": "complete history"}]
    assert called == {"agent_id": 42}


# ── Loki fetcher tests (mocked httpx) ─────────────────────────────────────


class _FakeResp:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _ErrResp(_FakeResp):
    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            "boom", request=httpx.Request("GET", "http://x"), response=httpx.Response(500)
        )


class _FakeClient:
    """Canned /api/events server: filters by (category, agent_id, ts window),
    pages newest-first like the real API, records every request."""

    def __init__(self, rows: list[dict[str, object]], *, error: bool = False) -> None:
        self.rows = rows
        self.error = error
        self.requests: list[tuple[str, dict[str, object], dict[str, object]]] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, object] | None = None,
        timeout: object = None,
    ) -> _FakeResp:
        p = dict(params or {})
        h = dict(headers or {})
        self.requests.append((url, p, h))
        if self.error:
            return _ErrResp({})
        cat = p.get("category")
        aid = p.get("agent_id")
        frm_raw, to_raw = p.get("from"), p.get("to")
        frm = frm_raw.isoformat() if isinstance(frm_raw, datetime) else str(frm_raw)
        to = to_raw.isoformat() if isinstance(to_raw, datetime) else str(to_raw)
        matching = [
            r
            for r in self.rows
            if r["category"] == cat
            and (aid is None or r["agent_id"] == aid)
            and frm <= str(r["ts"]) <= to
        ]
        total = len(matching)
        offset = int(cast(Any, p.get("offset", 0)))
        limit = int(cast(Any, p.get("limit", 1000)))
        page = sorted(matching, key=lambda r: str(r["ts"]), reverse=True)[offset : offset + limit]
        # 2026-08-18 contract: meta.total is opt-in (with_total=1) and the
        # gateway shed the count aggregation — collect paging must not depend
        # on it, so the fake omits `total` entirely. has_more drives bisection.
        return _FakeResp(
            {
                "items": page,
                "meta": {"has_more": offset + len(page) < total},
            }
        )


def _row(i: int, ts: datetime, category: str = "telemetry", agent_id: int = 7) -> dict[str, object]:
    return {
        "id": i,
        "ts": ts.isoformat(),
        "agent_id": agent_id,
        "category": category,
        "event_name": "turn_end",
        "attributes": {"i": i},
    }


_BASE = datetime.fromisoformat("2026-08-13T00:00:00+00:00")


def _ts_after(seconds: float) -> datetime:
    return _BASE + timedelta(seconds=seconds)


def _patch_client(collect_mod: Any, monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    def factory(timeout: Any = None) -> _FakeClient:
        return client

    monkeypatch.setattr(collect_mod.httpx, "Client", factory)


def test_fetch_slices_and_orders_oldest_first(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2500 rows > single-page budget -> binary-search time; one offset=0 query per slice
    rows = [_row(i, _ts_after(i * 2)) for i in range(2500)]
    client = _FakeClient(rows)
    _patch_client(collect_mod, monkeypatch, client)

    out = collect_mod._fetch_events_window("telemetry", _ts_after(0), _ts_after(7200))

    assert len(out) == 2500
    tss = [str(r["ts"]) for r in out]
    assert tss == sorted(tss)
    # No offset pagination: every request is offset=0 limit=1000 (Loki offset pagination times out / drops data)
    for _, p, _h in client.requests:
        assert p["offset"] == 0
        assert p["limit"] == 1000


def test_fetch_bisects_oversized_slices_without_loss(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 9000 rows across a 5-hour window -> over the 4000-row budget -> must split via binary search
    rows = [_row(i, _ts_after(i * 2)) for i in range(9000)]
    client = _FakeClient(rows)
    _patch_client(collect_mod, monkeypatch, client)

    out = collect_mod._fetch_events_window("telemetry", _ts_after(0), _ts_after(18000))

    assert len(out) == 9000
    assert len({r["id"] for r in out}) == 9000
    for _, p, _h in client.requests:
        assert p["offset"] == 0
        assert p["limit"] == 1000


def test_fetch_dedups_inclusive_slice_boundaries(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Boundary rows (exactly at the bisection midpoint) are included by both adjacent slices (window includes both ends) -> dedupe by id
    boundary = _row(999, _ts_after(1800))  # window midpoint = bisection cut, both ends include it
    rows = [_row(i, _ts_after(i * 2)) for i in range(2500)] + [boundary]
    client = _FakeClient(rows)
    _patch_client(collect_mod, monkeypatch, client)

    out = collect_mod._fetch_events_window("telemetry", _ts_after(0), _ts_after(3600))

    ids = [r["id"] for r in out]
    assert len(ids) == len(set(ids))
    assert ids.count(999) == 1


def test_fetch_passes_agent_filter_and_bearer_auth(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("AVA_CLUSTER_SECRET=test-secret\n")
    monkeypatch.setattr(collect_mod, "ava_home", lambda: tmp_path)
    client = _FakeClient([])
    _patch_client(collect_mod, monkeypatch, client)

    collect_mod._fetch_events_window("audit", _ts_after(0), _ts_after(3600), agent_id=42)

    assert client.requests
    for _, p, h in client.requests:
        assert p["agent_id"] == 42
        assert p["category"] == "audit"
        assert h == {"Authorization": "Bearer test-secret"}


def test_fetch_raises_on_http_error(collect_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([], error=True)
    _patch_client(collect_mod, monkeypatch, client)

    with pytest.raises(httpx.HTTPStatusError):
        collect_mod._fetch_events_window("telemetry", _ts_after(0), _ts_after(3600))


def test_fetch_requests_are_count_free(collect_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-08-18 contract lock: /api/events meta.total is opt-in behind
    with_total=1 because the count aggregation is exactly the Loki load the
    gateway shed. Paging decisions use has_more; no request may ask for the
    count (a re-introduced with_total=1 would silently re-add that load)."""
    rows = [_row(i, _ts_after(i * 2)) for i in range(2500)]
    client = _FakeClient(rows)
    _patch_client(collect_mod, monkeypatch, client)

    collect_mod._fetch_events_window("telemetry", _ts_after(0), _ts_after(7200))

    assert client.requests
    for _, p, _h in client.requests:
        assert "with_total" not in p


# ── plugin attribution (issue #40) ──────────────────────────────────────────


def test_plugins_activated_counts_per_contribution(collect_mod: Any) -> None:
    """The plugin-side counterpart of `skills_touched`: hooks and wraps act
    silently, so without this the dataset carried no evidence that a plugin
    touched a bad run. Keyed per contribution, not per plugin, so a regression
    points at the hook to read. Well-formed rows produce a zero skip count."""
    events = [
        (
            "plugin_activation",
            {
                "plugin": "ava_syntax_fix",
                "surface": "hooks",
                "identifier": "before_exec",
                "detail": "_SyntaxFixHook wrote messages",
                "model": "m",
            },
        ),
        (
            "plugin_activation",
            {
                "plugin": "ava_syntax_fix",
                "surface": "hooks",
                "identifier": "before_exec",
                "detail": "",
                "model": "m",
            },
        ),
        (
            "plugin_activation",
            {
                "plugin": "ava_code",
                "surface": "sdkWraps",
                "identifier": "files.read",
                "detail": "inner_calls=0",
                "model": "m",
            },
        ),
        ("turn_end", {"ok": True}),
    ]

    fired, skipped = collect_mod._plugins_activated(events)
    assert fired == {
        "ava_code/sdkWraps/files.read": 1,
        "ava_syntax_fix/hooks/before_exec": 2,
    }
    assert skipped == 0


def test_plugins_activated_counts_and_reports_malformed_rows(collect_mod: Any) -> None:
    """Issue #92: a row missing part of the key is still dropped (graceful
    drift — it never crashes the weekly collect), but the drop is now counted
    instead of vanishing silently, so an event-contract drift is visible on
    the run record rather than quietly thinning the dataset to empty."""
    events = [
        ("plugin_activation", {"plugin": "ava_code", "surface": "hooks"}),
        ("plugin_activation", None),
        ("plugin_activation", {}),
    ]

    fired, skipped = collect_mod._plugins_activated(events)
    assert fired == {}
    assert skipped == 3


def _fake_transcript(_agent_id: int) -> list[dict[str, str]]:
    return []


def _build_minimal_record(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch, events: list[tuple]
) -> dict[str, Any]:
    """build_record with every other input reduced to its empty case, so the
    test isolates the plugins_activated_skipped wiring."""
    monkeypatch.setattr(collect_mod, "_transcript", _fake_transcript)
    return collect_mod.build_record(
        agent_id=1,
        week="2026-W99",
        events=events,
        log_events=[],
        inbounds=[],
        meta=("user", "completed", "done"),
    )


def test_build_record_surfaces_plugins_activated_skipped(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #92: the skip count must land on the run record (not just the
    private counter), and a nonzero count must print a visible warning."""
    events = [
        ("plugin_activation", {"plugin": "ava_code", "surface": "hooks"}),  # malformed
        (
            "plugin_activation",
            {"plugin": "ava_code", "surface": "hooks", "identifier": "before_exec"},
        ),
    ]

    rec = _build_minimal_record(collect_mod, monkeypatch, events)

    assert rec["plugins_activated_skipped"] == 1
    assert rec["plugins_activated"] == {"ava_code/hooks/before_exec": 1}
    assert "skipped 1" in capsys.readouterr().out


def test_build_record_well_formed_rows_report_zero_skipped(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Well-formed rows must not be affected by the loud-skip change: zero
    skipped, no warning printed."""
    events = [
        (
            "plugin_activation",
            {"plugin": "ava_code", "surface": "hooks", "identifier": "before_exec"},
        ),
    ]

    rec = _build_minimal_record(collect_mod, monkeypatch, events)

    assert rec["plugins_activated_skipped"] == 0
    assert rec["plugins_activated"] == {"ava_code/hooks/before_exec": 1}
    assert capsys.readouterr().out == ""


def test_build_record_adds_leak_audit_only_for_eval_collection(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Weekly collection remains unchanged; eval collection records invalidating reads."""
    paths = collect_mod.LeakPaths(
        memory_pool=str(tmp_path / "memory"),
        self_evolution=str(tmp_path / "self_evolution"),
        workspaces_root=str(tmp_path / "workspaces"),
        agent_workspace=str(tmp_path / "workspaces" / "1"),
    )
    weekly = _build_minimal_record(
        collect_mod,
        monkeypatch,
        [("code", {"body": f"open('{paths.self_evolution}/reports/result.md')"})],
    )
    audited = collect_mod.build_record(
        agent_id=1,
        week="2026-W99",
        events=[("code", {"body": f"open('{paths.self_evolution}/reports/result.md')"})],
        log_events=[],
        inbounds=[],
        meta=("user", "completed", "done"),
        leak_paths=paths,
    )

    assert "leak_audit" not in weekly
    assert "invalidated" not in weekly
    assert audited["invalidated"] is True
    assert audited["leak_audit"][0]["surface"] == "results"


def test_test_label_ids_matches_prefix_only(collect_mod: Any, db_conn: psycopg.Connection) -> None:
    """Only TEST- prefixed role labels are test spawns: a plain agent label
    and a NULL label must stay in the dataset."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id, label) VALUES (9001, 'TEST-bench-on-t1'), "
            "(9002, 'test-agent-9002'), (9003, NULL)"
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        ids = collect_mod._test_label_ids(cur, [9001, 9002, 9003, 99999])

    assert ids == {9001}


def test_collect_with_counts_keeps_records_and_reports_filters(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """collect_with_counts() returns the same records as collect() plus the
    pre-record counts the daily sentinel keys on: distinct window agents
    seen, TEST- drops, missing-lifecycle drops (QA review of PR #698)."""
    rows: list[dict[str, object]] = [
        {"agent_id": 1, "event_name": "turn_end", "attributes": {}},
        {"agent_id": 2, "event_name": "turn_end", "attributes": {}},
        {"agent_id": 3, "event_name": "turn_end", "attributes": {}},
        {"agent_id": 4, "event_name": "turn_end", "attributes": {}},
    ]

    def _fetch(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return rows

    def _no_inbounds(*_args: object, **_kwargs: object) -> dict[int, Any]:
        return {}

    def _meta_for(*_args: object, **_kwargs: object) -> dict[int, tuple[str, str, str]]:
        return dict.fromkeys((1, 2, 3), ("user", "completed", "done"))

    def _test_ids(*_args: object, **_kwargs: object) -> set[int]:
        return {2}

    monkeypatch.setattr(collect_mod, "_fetch_events_window", _fetch)
    monkeypatch.setattr(collect_mod, "_inbounds_by_agent", _no_inbounds)
    monkeypatch.setattr(collect_mod, "_meta_by_agent", _meta_for)
    monkeypatch.setattr(collect_mod, "_test_label_ids", _test_ids)

    # collect() uses `with connect() as conn`, which closes the connection
    # at block exit (psycopg 3 context-manager semantics) — give each call a
    # fresh connection like the real pipeline gets.
    def _fresh_conn() -> psycopg.Connection:
        return psycopg.connect(collect_mod.settings.data_plane.db_url)

    monkeypatch.setattr(collect_mod, "connect", _fresh_conn)

    def _fake_build_record(agent_id: int, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"agent_id": agent_id, "label": "ok"}

    monkeypatch.setattr(collect_mod, "build_record", _fake_build_record)

    records, counts = collect_mod.collect_with_counts(1, "2026-08-26")

    assert counts == {"seen": 4, "excluded_test": 1, "skipped_meta": 1}
    assert [r["agent_id"] for r in records] == [1, 3]

    # include_test opts the TEST- spawns back in for measurement runs.
    records_all, counts_all = collect_mod.collect_with_counts(1, "2026-08-26", include_test=True)
    assert counts_all == {"seen": 4, "excluded_test": 0, "skipped_meta": 1}
    assert [r["agent_id"] for r in records_all] == [1, 2, 3]

    # The plain collect() wrapper stays records-only for existing callers.
    assert collect_mod.collect(1, "2026-08-26") == records
