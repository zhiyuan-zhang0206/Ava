"""Event-registry zero-enforcement gate — R2-C 单一事实源纪律.

`shared/events/contract.py` 的 `EVENTS` 是 event_name 唯一事实来源；本模块是
**验证者**（不是接缝——注册表之外不再有手工快照）。四个守卫：

1. **前向**：生产代码里每个静态 `event=` 字面量必须已注册。未注册 =
   `telemetry.emit` fail-fast（新代码）或静默落 category=log 30d（旧路径），
   二者都是事故。
2. **反向**：`EVENTS` 每个条目必须有生产者（`event=` / `label=` /
   `event_type=` 字面量，或下方 SQL/动态清单）——防删除/改名事件留下注册表
   残留（category 映射继续存活一个无人产生的事件）。
3. **文档漂移**：`shared/events/registry.md` 必须与生成器输出逐字一致（R2-C：
   文档是生成物）。
4. **SQL 键注入**：读取端 SQL 里的 attributes 键字面量（`->>'` 与 `?`
   两种形态）必须是一个 payload TypedDict 的声明键（`registered_payload_keys`）
   ——"改名事件 = 注册表一行 + 所有引用点编译/测试失败"的 SQL 半边。

限制（继承自 scan_kinds）：静态字面量扫描看不到动态产生的名字（变量
`event_type`、dict 值、三元表达式）——它们在 `_SQL_OR_DYNAMIC_KINDS` 里逐条
标注产生点。
"""

from __future__ import annotations

import re
from pathlib import Path

from shared.events import scan_kinds  # namespace package — pythonpath = ["."]
from shared.events.contract import EVENTS, registered_payload_keys, telemetry_events

_REPO = Path(__file__).resolve().parents[1]
_REGISTRY = _REPO / "shared" / "events" / "registry.md"

# Kinds whose production site has no static `event=` literal, so scan_kinds
# cannot see them. Each entry carries its emission site; removing a kind from
# the code means removing it here too (the reverse gate would otherwise flag
# the orphaned registry entry).
_SQL_OR_DYNAMIC_KINDS = frozenset(
    {
        # SQL 直写（绕过 loguru/emitter）——registry §3 标注。
        "delivery_stalled",  # services/delivery_watchdog/daemon.py:242
        "heartbeat_nudged",  # services/heartbeat/daemon.py:187
        # 动态 emit：位置参数形式，无 `event=` 字面量。
        "task_reminder_digest",  # task_maintenance/daemon.py:_run_reminders
        "task_escalation",  # task_maintenance/daemon.py:_run_escalate
        "heartbeat_paused",  # ava/self.py:258 telemetry.emit("telemetry", ...)
        "frontend_interaction",  # gateway/routers/frontend_telemetry.py telemetry.emit("telemetry", ...)
        "pgbouncer_repaired",  # services/healthchecks/pgbouncer.py:_emit_repaired
        "editable_pth_repaired",  # shared/editable_install.py:repair_editable_ava_pth
        "sdk_call",  # agent/sdk_metering.py recorder（经 shared/sdk_telemetry）
        # shared/plugin_activation.py:emit binds event=PLUGIN_ACTIVATION_EVENT (a
        # module constant, like sdk_call), so the literal scan cannot see it.
        "plugin_activation",
        "gateway_latency",  # gateway/_latency.py:emit_bucket telemetry.emit("telemetry", ...)
        "loki_query_budget",  # gateway/loki_query_budget.py:_emit_observation
        "telemetry_read_stale",  # gateway/telemetry_staleness.py:_emit
        "telemetry_read_recovered",  # gateway/telemetry_staleness.py:_emit
        "otlp_backend_disabled",  # shared/telemetry_otlp.py:_emit_backend_event
        "otlp_backend_recovered",  # shared/telemetry_otlp.py:_emit_backend_event
        "prom_query_budget",  # gateway/prom_metrics.py:_emit_budget_observation
        # Class-resolution markers select their name from the event level at
        # runtime; services/events_maintenance/resolution.py emits the reopen
        # markers and resolution_status, while the gateway emits resolved ones.
        "warning_resolved",
        "error_resolved",
        "warning_reopened",
        "error_reopened",
        "resolution_status",
        "checkpoint_table_sizes",  # services/events_maintenance/blob_vacuum.py telemetry.emit (positional)
        # 历史括号名：W8 改名前的旧值，仍是 migrate_events.py 的映射目标且
        # DB 有存量行。新代码禁止产生，保留注册只为回填口径。
        "exec(cancelled)",
        "exec(failed)",
        "exec(thread-stuck)",
        "exec(timeout)",
        # audit 动态 event_type：非 `event_type="x"` 字面量，扫描器看不到。
        "spawn",  # ops/agent_spawn.py:349 event_type = "fork" if ... else "spawn"
        "send_message",  # shared/db.py:497 inbound kind→event_type 映射值
        "terminate",  # shared/db.py:498 同上
        "cancel",  # shared/db.py:500 同上
        # 历史无生产者事件（registry §7.4）：DB 有存量行、schema 注释仍引用，
        # 保留注册待统一模型落地时确认退役。
        "report_activity",  # 无当前生产者（DB 5,274 行）
        "report_breached",  # 无当前生产者（DB 14 行）
    }
)


def _code_kinds() -> tuple[set[str], set[str], set[str]]:
    """(event= literals, label= literals, event_type= literals) from scan_kinds."""
    event_kinds, label_kinds, event_type_kinds, _sse_roles = scan_kinds.scan_code(_REPO)
    return set(event_kinds), set(label_kinds), set(event_type_kinds)


def test_every_static_event_kind_is_registered() -> None:
    """Forward gate: an `event=` kind outside `EVENTS` silently falls to
    category=log (30d retention) on the loguru path, and `telemetry.emit`
    fail-fasts on it elsewhere — either way it is a contract violation."""
    event_kinds, _, _ = _code_kinds()
    unregistered = sorted(k for k in event_kinds if k not in EVENTS)
    assert not unregistered, (
        "event= kind(s) missing from shared/events/contract.py EVENTS: "
        f"{unregistered}. Register each in the same PR that introduces it "
        "(one EventSpec line — shared/events/registry.md regenerates)."
    )


def test_registered_telemetry_events_have_producers() -> None:
    """Reverse drift guard: every EVENTS entry must have a code producer — an
    `event=` literal, a `label=` fallback, an `event_type=` literal, or a
    documented SQL/dynamic emission. Catches stale registry entries (deleted
    or renamed kind still listed) that would keep the category mapping alive
    for a kind nobody emits."""
    event_kinds, label_kinds, event_type_kinds = _code_kinds()
    produced = event_kinds | label_kinds | event_type_kinds | _SQL_OR_DYNAMIC_KINDS
    orphaned = sorted(telemetry_events() - produced)
    assert not orphaned, (
        "EVENTS telemetry kind(s) with no producer in code: "
        f"{orphaned}. Remove them from the registry, or add the emission site "
        "(or a `_SQL_OR_DYNAMIC_KINDS` entry with a comment) in the same PR."
    )


def test_registry_doc_matches_generated() -> None:
    """shared/events/registry.md is a generated artifact (R2-C): it must equal
    the generator's output byte-for-byte. A registry change without running
    `scripts/gen_event_registry.py` fails here (and in the pre-commit
    `events-registry-fresh` hook)."""
    from scripts.gen_event_registry import render  # namespace package

    generated = render()
    current = _REGISTRY.read_text(encoding="utf-8")
    assert current == generated, (
        "shared/events/registry.md is out of sync with the EVENTS registry — "
        "run .venv/bin/python scripts/gen_event_registry.py and commit the "
        "regenerated doc in the same PR."
    )


_ATTRIBUTES_KEY_RE = re.compile(r"""attributes(?:->>|\s*\?\s*)'([^']+)'""")

# The SQL-fragment definition site — `_sql_keys` builds `attributes->>'<key>'`
# from registered payload keys; every other file must not hand-write literals.
_EXEMPT_SQL_KEY_FILES = frozenset({"shared/events/contract.py"})


def test_attributes_key_literals_are_registered() -> None:
    """SQL key-injection gate: every attributes key literal (the `->>`
    and `?` forms) in the repo must be a declared payload key.

    A reader referencing a key no producer declares is a contract violation,
    not a query detail — it would silently NULL out after a payload rename.
    New read sites should consume the per-event SQL fragment constants
    (LLM_USAGE_KEYS etc.) from shared/events/contract.py instead of writing
    literals; this gate is the safety net for hand-written ones.
    """
    unregistered: list[tuple[str, int, str]] = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if any(
            seg in rel
            for seg in (
                ".venv",
                ".git",
                ".worktrees",
                "__pycache__",
                ".ruff_cache",
                ".pytest_cache",
            )
        ):
            continue
        if rel in _EXEMPT_SQL_KEY_FILES:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue
            for match in _ATTRIBUTES_KEY_RE.finditer(line):
                key = match.group(1)
                if key not in registered_payload_keys():
                    unregistered.append((rel, lineno, key))
    assert not unregistered, (
        "attributes key literal(s) not declared in any payload TypedDict "
        f"(shared/events/contract.py): {unregistered[:10]}. Declare the key "
        "on the event's payload TypedDict, or use the registry SQL fragment "
        "constants."
    )
