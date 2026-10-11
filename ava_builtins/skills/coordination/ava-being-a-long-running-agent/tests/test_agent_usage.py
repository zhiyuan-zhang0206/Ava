"""Real SQL coverage for selectable usage scopes and notification-only budgets."""

import importlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.metrics import usage as reader

usage = importlib.import_module(
    "ava_builtins.skills.coordination.ava-being-a-long-running-agent.scripts.agent_usage"
)


def test_polling_keeps_one_captured_entry_gate(
    database: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    image = LoadedCommit(Path("installed-source"), "loaded-image")
    captures: list[LoadedCommit] = []
    gates: list[ProcessDbGate] = []
    reports = iter([0.0, 1.0])
    notified: list[tuple[int, ...]] = []

    def capture() -> LoadedCommit:
        captures.append(image)
        return image

    def version(owner: CodeVersion) -> int:
        assert owner.loaded is image
        return 18

    def build(*, gate: ProcessDbGate) -> Database:
        gates.append(gate)
        return database

    def report(_conn: psycopg.Connection, **_kwargs: object) -> dict[str, Any]:
        return {
            "totals": {"recorded_cost_usd": next(reports), "unpriced_calls": 0},
            "agents": [{"agent_id": 1}],
            "lineage": "self",
            "start": "start",
            "end": "end",
        }

    args = usage.parse_args(
        [
            "--agent-id",
            "1",
            "--lifetime",
            "--usd-limit",
            "1",
            "--notify-agent",
            "2",
            "--poll-seconds",
            "1",
        ]
    )
    monkeypatch.setattr(usage, "parse_args", lambda: args)
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(CodeVersion, "get", version)
    monkeypatch.setattr(Database, "from_settings", build)
    monkeypatch.setattr(usage, "usage_report", report)

    def sleep(_seconds: float) -> None:
        pass

    def notify(peers: Sequence[int], _message: str) -> None:
        notified.append(tuple(peers))

    monkeypatch.setattr(usage.time, "sleep", sleep)
    monkeypatch.setattr(usage, "notify_agents", notify)
    usage.main()
    assert captures == [image]
    assert len(gates) == 1
    assert gates[0].application_name().endswith(":v18")
    assert gates[0].min_read_due()
    assert notified == [(2,)]
    assert len(capsys.readouterr().out.splitlines()) == 2


def seed_agent(conn: psycopg.Connection, parent: int | None = None, *, fork: bool = False) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    spawner = "user" if parent is None else f"agent:{parent}"
    conn.execute(
        "INSERT INTO agents_meta (id, status, spawner, born_spawner, fork_source_agent_id, fork_source_checkpoint_id) VALUES (%s,'idling',%s,%s,%s,%s)",
        (agent_id, spawner, spawner, parent if fork else None, "test-checkpoint" if fork else None),
    )
    return agent_id


def event(
    conn: psycopg.Connection, agent_id: int, ts: datetime, attributes: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO telemetry_events (event_uid,ts,agent_id,machine,cluster,process,category,event_name,level,source,attributes) "
        "VALUES (%s,%s,%s,'test','test','test','telemetry','llm_usage','info','test',%s)",
        (agent_id * 1_000_000 + int(ts.timestamp()), ts, agent_id, Jsonb(attributes)),
    )


def test_birth_lineage_modes_overlap_and_folding(db_conn: psycopg.Connection) -> None:
    root = seed_agent(db_conn)
    child = seed_agent(db_conn, root)
    fork = seed_agent(db_conn, root, fork=True)
    mixed = seed_agent(db_conn, child, fork=True)
    grandchild = seed_agent(db_conn, child)
    fork_child = seed_agent(db_conn, fork)
    other = seed_agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET spawner = 'user' WHERE id = %s", (child,))
    assert reader.select_agents(db_conn, [root], "self") == [root]
    assert reader.select_agents(db_conn, [root], "spawn") == sorted([root, child, grandchild])
    assert reader.select_agents(db_conn, [root], "fork") == sorted([root, fork])
    assert reader.select_agents(db_conn, [root, child], "all") == sorted(
        [root, child, fork, mixed, grandchild, fork_child]
    )
    assert other not in reader.select_agents(db_conn, [root], "all")
    with pytest.raises(ValueError, match="unknown agent IDs"):
        reader.select_agents(db_conn, [999_999_999], "all")
    with pytest.raises(ValueError, match="unknown lineage"):
        reader.select_agents(db_conn, [root], "unknown")  # pyright: ignore[reportArgumentType]


def test_window_prices_zero_usage_and_unpriced_events(db_conn: psycopg.Connection) -> None:
    root = seed_agent(db_conn)
    child = seed_agent(db_conn, root)
    idle = seed_agent(db_conn, root)
    other = seed_agent(db_conn)
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    end = start + timedelta(minutes=4)
    priced = {"in_total": 100, "out_total": 20, "cache_read": 60, "cost_usd": 0.25}
    event(db_conn, root, start - timedelta(seconds=1), priced)  # outside lower boundary
    event(db_conn, root, start, priced)
    event(
        db_conn,
        child,
        end - timedelta(seconds=1),
        {"in_total": 30, "out_total": 5, "cache_read": 0, "unpriced": 1},
    )
    event(db_conn, child, end + timedelta(seconds=1), priced)
    event(db_conn, other, end, priced)
    report = reader.usage_report(db_conn, roots=[root, child], lineage="all", start=start, end=end)
    assert report["totals"] == {
        "calls": 2,
        "input_tokens": 130,
        "output_tokens": 25,
        "cache_read_tokens": 60,
        "reasoning_tokens": 0,
        "total_tokens": 155,
        "recorded_cost_usd": 0.25,
        "unpriced_calls": 1,
    }
    assert next(row for row in report["agents"] if row["agent_id"] == idle)["calls"] == 0
    with pytest.raises(ValueError, match="start < end"):
        reader.usage_report(db_conn, roots=[root], lineage="self", start=end, end=start)


def test_budget_reminds_named_peers_without_lifecycle_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ava.agents

    sent: list[tuple[int, str]] = []

    def record(agent_id: int, message: str) -> None:
        sent.append((agent_id, message))

    def refuse_termination(*_args: object, **_kwargs: object) -> None:
        pytest.fail("budget reminder must not terminate")

    monkeypatch.setattr(ava.agents, "send_message", record)
    monkeypatch.setattr(ava.agents, "terminate", refuse_termination)
    report: dict[str, Any] = {
        "totals": {"total_tokens": 100, "recorded_cost_usd": 0.25, "unpriced_calls": 1},
        "agents": [{"agent_id": 1}],
        "lineage": "self",
        "start": "start",
        "end": "end",
    }
    assert usage.budget_message(report, usd_limit=1.0) is None
    assert usage.budget_message(report, usd_limit=None) is None
    report["totals"]["total_tokens"] = 1_000_000_000
    assert usage.budget_message(report, usd_limit=1.0) is None
    report["totals"]["total_tokens"] = 0
    message = usage.budget_message(report, usd_limit=0.25)
    assert "handoff" in message and "Unpriced calls: 1" in message
    usage.notify_agents([20, 10, 20], message)
    assert [row[0] for row in sent] == [10, 20]
    assert "recorded USD 0.250000 >= 0.25" in message
    for limit in (float("nan"), float("inf"), -1, 0, True):
        with pytest.raises(ValueError, match="positive and finite"):
            usage.budget_message(report, usd_limit=limit)
    with pytest.raises(ValueError, match="timezone"):
        usage.parse_time("2026-10-06T12:00:00")


@pytest.mark.parametrize(
    "options",
    [
        ["--lineage", "unknown"],
        ["--end", "2026-10-06T00:00:00+00:00"],
        ["--poll-seconds", "30"],
        ["--notify-agent", "2"],
        ["--poll-seconds", "nan", "--usd-limit", "1", "--notify-agent", "2"],
        ["--token-limit", "100"],
    ],
)
def test_cli_rejects_invalid_observation_modes(options: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        usage.parse_args(["--agent-id", "1", "--lifetime", *options])
    assert exc.value.code == 2


def test_cli_reads_a_real_window_and_sends_one_reminder(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import sys

    root = seed_agent(db_conn)
    start = datetime.now(UTC) - timedelta(minutes=1)
    event(
        db_conn,
        root,
        start + timedelta(seconds=1),
        {"in_total": 100, "out_total": 20, "cost_usd": 0.1},
    )
    db_conn.commit()
    messages: list[tuple[list[int], str]] = []

    def record(recipients: list[int], message: str) -> None:
        messages.append((recipients, message))

    monkeypatch.setattr(usage, "notify_agents", record)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent_usage.py",
            "--agent-id",
            str(root),
            "--start",
            start.isoformat(),
            "--usd-limit",
            "0.1",
            "--notify-agent",
            str(root),
        ],
    )
    usage.main()
    result = json.loads(capsys.readouterr().out)
    assert result["totals"]["total_tokens"] == 120
    assert len(messages) == 1
    assert messages[0][0] == [root]
    assert "handoff" in messages[0][1]


def test_lifetime_report_combines_ledger_and_tail(db_conn: psycopg.Connection) -> None:
    root = seed_agent(db_conn)
    now = datetime.now(UTC)
    yesterday = now.date() - timedelta(days=1)
    db_conn.execute(
        "INSERT INTO agent_model_tokens_daily (agent_id,day,model,llm_calls,costed_calls,unpriced_calls,tokens_in,tokens_out,cost_usd) "
        "VALUES (%s,%s,'m',2,2,0,100,20,0.5)",
        (root, yesterday),
    )
    event(
        db_conn,
        root,
        now - timedelta(seconds=1),
        {"model": "m", "in_total": 10, "out_total": 2, "cost_usd": 0.1},
    )
    report = reader.usage_report(db_conn, roots=[root], lineage="self", start=None, end=now)
    assert report["totals"]["calls"] == 3
    assert report["totals"]["total_tokens"] == 132
    assert report["totals"]["recorded_cost_usd"] == 0.6
    assert report["agents"][0]["by_model"]["m"]["llm_calls"] == 3
