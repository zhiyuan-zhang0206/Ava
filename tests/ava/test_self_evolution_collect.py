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
        _insert_inbound(cur, 1, "user", "old correction 错了", "8 days")
        _insert_inbound(cur, 1, "user", "window follow-up", "1 hour")
        _insert_inbound(cur, 2, "user", "task prompt 2", "30 days")
        _insert_inbound(cur, 2, "agent:99", "old peer ping 别忘了", "29 days")
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
        return _FakeResp(
            {
                "items": page,
                "meta": {"total": total, "has_more": offset + len(page) < total},
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
    # 2500 行 > 单页预算 → 时间二分；每片一次 offset=0 查询
    rows = [_row(i, _ts_after(i * 2)) for i in range(2500)]
    client = _FakeClient(rows)
    _patch_client(collect_mod, monkeypatch, client)

    out = collect_mod._fetch_events_window("telemetry", _ts_after(0), _ts_after(7200))

    assert len(out) == 2500
    tss = [str(r["ts"]) for r in out]
    assert tss == sorted(tss)
    # 无 offset 分页：每个请求都是 offset=0 limit=1000（Loki offset 分页会超时/丢数据）
    for _, p, _h in client.requests:
        assert p["offset"] == 0
        assert p["limit"] == 1000


def test_fetch_bisects_oversized_slices_without_loss(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 9000 行分布在 5 小时窗口 → 总行数超 4000 预算 → 必须二分切小
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
    # 边界行（恰好落在二分中点）会被相邻两片同时包含（窗口两端皆含）→ 按 id 去重
    boundary = _row(999, _ts_after(1800))  # 窗口中点 = 二分切点，两端皆含
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
