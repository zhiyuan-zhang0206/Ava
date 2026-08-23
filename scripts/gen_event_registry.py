# ruff: noqa: RUF001 — generated doc keeps a few non-ASCII glyphs (✓/—); deliberate
"""Generate shared/events/registry.md from the event contract registry (R2-C).

Single source of truth: ``shared/events/contract.EVENTS`` (name x category x
payload TypedDict x retention x destination) plus the SSE roles in
``shared/live_events.py``. The generated doc carries the registry data as
tables and preserves the governance prose (§6-§8) verbatim; producer /
consumer provenance now lives at the emit sites in code, not in this doc.

Run manually after changing EVENTS / live_events roles, or let pre-commit's
`events-registry-fresh` hook fail-loud on drift:
    .venv/bin/python scripts/gen_event_registry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from shared.events.contract import (
    EVENTS,
    RETENTION_BY_CATEGORY,
    payload_keys,
    retention_days,
)
from shared.live_events import GLOBAL_ROLES, SYSTEM_ROLES

_OUT = Path("shared/events/registry.md")

_HEADER = """# Ava event name registry

> This document is **generated**: produced by `scripts/gen_event_registry.py` from
> the `EVENTS` registry in `shared/events/contract.py` (since 2026-08-08, R2-C).
> Hand edits are flagged as drift by the pre-commit `events-registry-fresh` hook.
> Inventory method and history: see §8.

> **Terminology (aligned to the OTel 2026 model on 2026-08-06, task #898)**: `kind` is
> the old name — in the unified event model every event is a named LogRecord, and the
> name field is OTel `event.name` (code and the DB column are unified as
> `event_name`). Each entry in this document = one `event_name` value; the SSE-side
> discriminator is called `role` (§5, live projection, not persisted).

**The registry is the single source of truth for event_name** (the `EVENTS` in
`shared/events/contract.py`). A new event = one row added to the registry
(`telemetry.emit` fails fast on unregistered names); the tables in this document are
generated from it and never hand-synced. event_names that violate the naming rules
(§6) must not enter code.

---

## 0. Quick overview (numbers)

| mechanism | table/channel | registered event_names | destination | retention |
|------|------|-------------|------|------|
| audit (category=audit) | `events` | {n_audit} | events table | {ret_audit}d+ |
| telemetry (category=telemetry) | `events` | {n_telemetry} | events table | {ret_telemetry}d |
| log (category=log) | `events` | {n_log} | events table | {ret_log}d |
| file-only (destination=file) | file log | {n_file} | file only (not the events table) | — |
| SSE live | Redis → frontend (not persisted) | {n_sse} role | live projection | ephemeral |

All persistent events land in the single `events` table (`category` distinguishes
audit / telemetry / log). The four legacy mechanisms under the unified event model
(in one sentence): **audit = the category=audit part of the event river;
telemetry + log = the category=telemetry + log parts; SSE = a live projection of the
latest drops of the event river; OTel trace = the call-chain view of the event
river** (trace_id ties them together). Full treatment in
[event-system design decision](../../decisions/2026-08-04-event-system-design.md).

---

## 1. Category split

The unified model's `category` field drives retention, query permissions, and
monitoring alerts (design doc §5):

| category | what it is | destination | target retention |
|----------|--------|---------|---------|
| `audit` | business-operation facts: who did what to whom (spawn/message/task/status) | `events` | {ret_audit}d+ (immutable, append-only) |
| `telemetry` | runtime observations: token, turn, exec, node, health, delivery | `events` | {ret_telemetry}d |
| `log` | bare log lines (logger calls without event=/label=) | `events` | {ret_log}d (currently INFO+ persisted; design intent: L2 = WARNING+ only, see §7.2) |

SSE roles are not persisted and have no retention concept — live projection; OTel
spans go through the trace channel (30d).

---
"""

_AUDIT_INTRO = """
## 2. Audit events ({primary_n} primary category=audit; {n} status_change with extra_categories)

**Meaning convention**: category=audit rows are append-only operation audits, one row
= one agent operation fact. `source` (who triggered: `agent:N` / `user` / `system` /
`self`) and `target_agent_id` (against whom) are the two key audit dimensions, queried
more often than payload. Payload keys other than those listed have no Pydantic model
(display-surface use; see the payload tiering rules in `shared/audit_events.py`).
Emit sites and consumers: see the comments at each emit point.

| event_name | meaning | tier | key payload fields | retention | destination |
|------|------|------|-----------------|------|------|
"""

_TELEMETRY_INTRO = """
## 3. Telemetry events (category=telemetry, {n})

Telemetry-side event name resolution (`shared/log.py`): **explicit `event=` →
`label=` fallback → default `"log"`**. Payload = logger extra fields + `msg`
(formatted full text) + exception traceback. `(L)` marks names currently produced via
**label fallback** (no explicit event=); `(SQL)` marks writes that bypass loguru and
write SQL directly — both are annotated in the registry doc field. Emit sites and
consumers: see the comments at each emit point.

| event_name | meaning | tier | key payload fields | family | retention | destination |
|------|------|------|-----------------|----|------|------|
"""

_LOG_INTRO = """
## 4. Log (bare logs, category=log)

| event_name | meaning | tier | key payload fields | retention | destination |
|------|------|------|-----------------|------|------|
"""

_SSE_INTRO = """
## 5. SSE roles (live channel, not persisted, {n})

Typed Pydantic discriminators in `shared/live_events.py` (role is a Literal);
`EVENT_ADAPTER` / `SYSTEM_ROLES` / `GLOBAL_ROLES` derive from the single
`_ROLE_CLASSES` registry (R2-C). SSE is a **live projection** of the "latest drops"
of the event river — not persisted, unlike persistent events; role naming shares the
same origin as persistent events but uses an independent schema.

| role | global broadcast (/api/system) |
|------|------|
"""

_GOVERNANCE = """
---

## 6. Naming rules (mandatory for new event_names)

Under the unified model `event_name` is a globally unique event name (OTel
`event.name`). Rules:

1. **Format: `<domain>_<action>`, lowercase snake_case**. Domain = the subject domain
   (`llm`, `exec`, `db`, `agent`, `task`, `sse`, `label`, `launch`, `respawn`,
   `checkpoint`, `stream`, `service`, `boot`, `process`, `heartbeat`...), action =
   past tense or noun (`usage`, `end`, `enter`, `exit`, `failed`, `paused`,
   `dropped`...).
   Examples: `llm_usage` ✓, `db_pool_acquire_timeout` ✓, `exec(timeout)` ✗.
2. **No bare `log`**: every logger call must carry an explicit `event=`; `label=` is a
   UI display field only and must not be reused as an event name (the fallback
   remains only for migration-period compatibility).
3. **No dynamic event_name values**: event_name must come from a static literal or an
   in-code registry constant. Dynamic dimensions (agent_id, machine, node, status)
   always go into `attributes`/payload, **never into the event_name string**.
4. **No punctuation beyond underscores**: event_name charset is `[a-z0-9_]`, no
   parentheses.
5. **One fact = one event_name, no cross-category near-duplicates**: when the same
   fact exists in both audit and telemetry, distinguish by category, do not mint a
   near-synonym name.
6. **New event_names must be registered in `EVENTS` in `shared/events/contract.py`
   first** (this document is generated from it; registration = documented), and the
   PR description cites the registry entry.
7. **Payload tiering**: only event_names whose payload fields are **branched on** by
   downstream programs get Pydantic models; display-surface payloads stay untyped
   dicts (tiering rules in `shared/audit_events.py`).

---

## 7. Noise and governance record (by priority)

### 7.1 [P0] Dynamic-label pseudo event_names — ✓ governed (W8, 2026-08-04)

Before: `status (agent N)` (5,626), `idle wake (agent N)` (2,180), `claim (agent N)`
(1,016) — label fallback spliced agent_id into the event name, exploding the event
value space (468 distinct vs ~60 static), so any stats/alerts keyed by event_name
broke.

**Governance done (W8 · PR #1348)**:
- the 3 dynamic labels in `agent/db.py` → explicit `event="status_change"` (×2,
  payload carries from/to) and `event="idle_wake"` (payload carries
  degraded/elapsed_s/rounds/timeout_s); label keeps its UI display role
  (`status-change` / `idle-wake`).
- `claim (agent N)` was already eliminated by the earlier #1186 refactor (no new
  rows since 8/1); no residue.
- existing historical rows (~75k) stay as-is; new data is all static event_name.

**Re-run verification**: the event=/label= inventory from `scan_kinds.py --repo .`
shows no dynamic concatenation left.

### 7.2 [P0] Bare logs are 50.8% (3.07M / 6.04M rows)

Open item from design doc §8.2: L2 only accepts WARNING+. Today INFO bare logs are
pure filtering burden for metrics/rollup. **Governance**: downgrade the DB sink
(INFO stays in JSONL), require explicit event=.

### 7.3 [P1] Cross-category duplicates / naming inconsistencies — ✓ governed (W8, 2026-08-04)

- `compact` (audit) vs the agent_events label fallback `compact` — **governed**:
  agent_events now uses `compact_request`, audit keeps `compact`.
- `resurrect` (audit) vs `agent_resurrected` (telemetry) overlap semantically —
  **coordinated**: names were already distinct, kept apart by category.
- Parenthesized names `llm(cancelled)` / `exec(timeout)` → `llm_cancelled` /
  `exec_timeout` — **governed**.
- Hyphenated names `recall-filter` / `checkpoint-trim` / `compact-reminder` →
  `recall_filter` / `checkpoint_trim` / `compact_reminder` — **governed**.
- `llm-provider-error` and `llm_provider_error` are two names for one fact —
  **merged**: the label display name is unified as `llm_provider_error`, one event
  name.

### 7.4 [P2] Historical event_names with no producer

`report_activity` (5,274 rows) and `report_breached` (14 rows) have no producer left
in code, but the schema comment and the self-evolution collector still reference
them. Confirm keep/retire when the unified model lands.

### 7.5 [Resolved] skill_invoked parse/injection noise

Both historical sources eliminated: `invocation_depth=prompt_injected` is no longer
written; node parsing no longer records it since 2026-08-06 — the event is only
produced when the SKILL.md body is first consumed (`help()` / `__doc__`), so the
loaded semantics = the body was actually read.

### 7.6 [Settled] Final event_name-category caliber (2026-08-05, tracker #762/#763)

The `EVENTS` registry in `shared/events/contract.py` (R2-C) is the final caliber for
event_name and category (telemetry 90d / log 30d); `_TELEMETRY_KINDS` in
`shared/telemetry.py` is a derived projection, no longer hand-maintained:

- **`text`, `syntax_fix` → telemetry**: the whitelist previously missed these two
  label-fallback event_names, so live data landed in log (30d). Now whitelisted +
  historical rows (~103k) backfilled to telemetry by migration
  `20260805T083741_kind-category-final`.
- **`agent_restarted` → telemetry** (#763): whitelisted in W8; live and migration
  agree.
- **Historical/dead event_names stay log**: `claim`, `code(cancelled)`,
  `exec(interrupt)`, `stream_corrupted_retry`,
  `stream_corrupted_or_stalled_retry`, `watcher_resume_recovery`,
  `report_overdue_nudged`, `report_paused` — no producer, no consumer, not
  whitelisted (natural retirement at 30d).
- **`recall_filter` panel fixed**: `ava_builtins/plugins/ava_memory/metrics.py`
  previously queried the old spelling `recall-filter` (0 rows since the W8 rename),
  now queries `recall_filter` + telemetry.

---

## 8. Inventory method and coverage (reproducible)

1. **Registry**: the `EVENTS` in `shared/events/contract.py` is the single source of
   truth; this document is generated by `scripts/gen_event_registry.py` (the
   pre-commit `events-registry-fresh` hook verifies no drift).
2. **Code-literal cross-check**: `python shared/events/scan_kinds.py` — scans
   production Python code (excluding tests/worktrees/node_modules) for `event=`,
   `label=`, `event_type=`, SSE roles, emits four inventories, and cross-checks them
   bidirectionally against the registry (`tests/test_lint_event_kinds.py`).
3. **DB-measured distribution** (optional): `python shared/events/scan_kinds.py --db-url "$AVA_DB_URL"`
   — appends the events table's event_name distribution and bare-log share (all
   read-only SELECTs).
4. **Coverage statement**: this document covers every registered event_name (audit +
   telemetry + log + destination=file + SSE role). Not covered: test-fixture
   self-made names like `evt`/`my_event`/`some_warning` (non-production events).

**CI wiring**: `tests/test_lint_event_kinds.py` is a three-way guard — every static
`event=` literal in code must be registered (in `EVENTS`), every registry entry must
have a producer (event=/label=/SQL/dynamic inventories), and this document must match
the generator output (drift = red). Emit-side fail-fast additionally raises
`ValueError` for unregistered names.

**When to regenerate**: run the generator before any PR that adds/renames an
event_name or SSE role; the pre-commit hook blocks drift.
"""


def _row(name: str) -> str:
    """One registry table row for `name`."""
    spec = EVENTS[name]
    keys = ", ".join(payload_keys(name)) if payload_keys(name) else "—"
    ret = f"{retention_days(name)}d"
    dest = "file" if spec.destination == "file" else "events"
    return f"| `{name}` | {spec.doc} | {spec.tier} | {keys} | {ret} | {dest} |"


def _row_telemetry(name: str) -> str:
    spec = EVENTS[name]
    keys = ", ".join(payload_keys(name)) if payload_keys(name) else "—"
    ret = f"{retention_days(name)}d"
    dest = "file" if spec.destination == "file" else "events"
    fam = spec.family or "—"
    return f"| `{name}` | {spec.doc} | {spec.tier} | {keys} | {fam} | {ret} | {dest} |"


def render() -> str:
    audit = [n for n, s in EVENTS.items() if s.category == "audit"]
    telemetry = [n for n, s in EVENTS.items() if s.category == "telemetry"]
    log = [n for n, s in EVENTS.items() if s.category == "log"]
    file_only = [n for n, s in EVENTS.items() if s.destination == "file"]

    out = _HEADER.format(
        n_audit=len(audit),
        n_telemetry=len(telemetry),
        n_log=len(log),
        n_file=len(file_only),
        n_sse=len(SYSTEM_ROLES),
        ret_audit=RETENTION_BY_CATEGORY["audit"],
        ret_telemetry=RETENTION_BY_CATEGORY["telemetry"],
        ret_log=RETENTION_BY_CATEGORY["log"],
    )

    out += _AUDIT_INTRO.format(
        n=len(audit),
        primary_n=len([n for n, s in EVENTS.items() if s.category == "audit"]),
    )
    for name in audit:
        out += _row(name) + "\n"

    out += _TELEMETRY_INTRO.format(n=len(telemetry))
    for name in telemetry:
        out += _row_telemetry(name) + "\n"

    out += _LOG_INTRO
    for name in log:
        out += _row(name) + "\n"

    out += _SSE_INTRO.format(n=len(SYSTEM_ROLES))
    for role in sorted(SYSTEM_ROLES):
        flag = "✓" if role in GLOBAL_ROLES else "—"
        out += f"| `{role}` | {flag} |\n"

    out += _GOVERNANCE
    return out


def main(*, check: bool = False, out: str | None = None) -> int:
    rendered = render()
    target = Path(out) if out else _OUT
    if check:
        current = target.read_text(encoding="utf-8")
        if current != rendered:
            print(
                "ERROR: shared/events/registry.md is out of sync with the registry.\n"
                "   run .venv/bin/python scripts/gen_event_registry.py to regenerate"
            )
            return 1
        print(f"{target} is up to date")
        return 0
    target.write_text(rendered)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(check="--check" in sys.argv, out=args[0] if args else None))
