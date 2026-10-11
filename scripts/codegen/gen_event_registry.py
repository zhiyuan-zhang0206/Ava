"""Generate base/events/registry.md from the event contract registry (R2-C).

Single source of truth: ``base/events/contract.EVENTS`` (name x category x
payload TypedDict x destination) plus the SSE roles in
``base/events/live/projection.py``. The generated doc carries the registry data as
tables and preserves the governance prose (§6-§8) verbatim; producer /
consumer provenance now lives at the emit sites in code, not in this doc.

Run manually after changing EVENTS / live_events roles, or let pre-commit's
`events-registry-fresh` hook fail-loud on drift:
    .venv/bin/python scripts/codegen/gen_event_registry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put project root on sys.path so `from base... import ...` finds this
# checkout's modules (without it, an editable install of another checkout
# shadows them — same pattern as dump_event_fixtures.py / dump_openapi.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()


from base.events.contract import EVENTS, payload_keys
from base.events.live.projection import GLOBAL_ROLES, SYSTEM_ROLES

_OUT = Path("base/events/registry.md")

_HEADER = """# Ava event name registry

> This document is **generated**: produced by `scripts/codegen/gen_event_registry.py` from
> the `EVENTS` registry in `base/events/contract.py`, merged from the domain modules of
> `base/events/declarations/` (since 2026-08-08, R2-C).
> Hand edits are flagged as drift by the pre-commit `events-registry-fresh` hook.
> Inventory method and history: see §8.

> **Terminology (aligned to the OTel 2026 model on 2026-08-06, task #898)**: `kind` is
> the old name — in the unified event model every event is a named LogRecord, and the
> name field is OTel `event.name` (code and the DB column are unified as
> `event_name`). Each entry in this document = one `event_name` value; the SSE-side
> discriminator is called `role` (§5, live projection, not persisted).

**The registry is the single source of truth for event_name** (the `EVENTS` in
`base/events/contract.py`). A new event = one `EventSpec` in its domain module under
`base/events/declarations/` (`telemetry.emit` fails fast on unregistered names); the tables in this document are
generated from it and never hand-synced. event_names that violate the naming rules
(§6) must not enter code.

---

## 0. Quick overview

| mechanism | table/channel | destination |
|------|------|------|
| audit (category=audit) | `events` | event stream |
| telemetry (category=telemetry) | `events` | event stream |
| log (category=log) | `events` | event stream |
| file-only (destination=file) | file log | file only (not the stream) |
| SSE live | Redis → frontend (not persisted) | live projection |

All persistent events land in the unified event stream (`category` distinguishes
audit / telemetry / log). The four legacy mechanisms under the unified event model
(in one sentence): **audit = the category=audit part of the event river;
telemetry + log = the category=telemetry + log parts; SSE = a live projection of the
latest drops of the event river; OTel trace = the call-chain view of the event
river** (trace_id ties them together). Full treatment in
[event-system design decision](../../docs/decisions/observability/events/2026-08-04-event-system-design.md).

---

## 1. Category split

The unified model's `category` field drives query permissions and
monitoring alerts (design doc §5):

| category | what it is | destination |
|----------|--------|---------|
| `audit` | business-operation facts: who did what to whom (spawn/message/task/status) | `events` |
| `telemetry` | runtime observations: token, turn, exec, node, health, delivery | `events` |
| `log` | bare log lines (logger calls without event=) | `events` |

SSE roles are not persisted and have no retention concept — live projection; OTel
spans go through the trace channel (30d).

---
"""

_AUDIT_INTRO = """
## 2. Audit events

**Meaning convention**: category=audit rows are append-only operation audits, one row
= one agent operation fact. `source` (who triggered: `agent:N` / `user` / `system` /
`self`) and `target_agent_id` (against whom) are the two key audit dimensions, queried
more often than payload. Payload keys other than those listed have no Pydantic model
(display-surface use; see the payload tiering rules in `base/telemetry/audit_events.py`).
Emit sites and consumers: see the comments at each emit point.

| event_name | meaning | tier | key payload fields | destination |
|------|------|------|-----------------|------|
"""

_TELEMETRY_INTRO = """
## 3. Telemetry events (category=telemetry)

Telemetry-side event name resolution (`base/log/process.py`): **explicit `event=` →
default `"log"`**. Payload = logger extra fields + `msg`
(formatted full text) + exception traceback. `(SQL)` marks writes that bypass loguru and
write SQL directly — annotated in the registry doc field. Emit sites and
consumers: see the comments at each emit point.

**Stored in `telemetry_events`**: the `stored` column is `EventSpec.persist` — a Postgres
reader queries the event by name. An event without it is stored only at warning level or
above (or when its name is unregistered); otherwise it stays in the JSONL mirror and Loki
(`base/telemetry/event_store.py: is_persisted`).

| event_name | meaning | tier | key payload fields | family | destination | stored |
|------|------|------|-----------------|----|------|----|
"""

_LOG_INTRO = """
## 4. Log (bare logs, category=log)

| event_name | meaning | tier | key payload fields | destination |
|------|------|------|-----------------|------|
"""

_SSE_INTRO = """
## 5. SSE roles (live channel, not persisted)

Typed Pydantic discriminators in `base/events/live/projection.py` (role is a Literal);
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
   UI display field only and never names an event.
3. **No dynamic event_name values**: event_name must come from a static literal or an
   in-code registry constant. Dynamic dimensions (agent_id, machine, node, status)
   always go into `attributes`/payload, **never into the event_name string**.
4. **No punctuation beyond underscores**: event_name charset is `[a-z0-9_]`, no
   parentheses.
5. **One fact = one event_name, no cross-category near-duplicates**: when the same
   fact exists in both audit and telemetry, distinguish by category, do not mint a
   near-synonym name.
6. **New event_names must be declared in a `base/events/declarations/` module
   first** (this document is generated from it; registration = documented), and the
   PR description cites the registry entry.
7. **Payload tiering**: only event_names whose payload fields are **branched on** by
   downstream programs get Pydantic models; display-surface payloads stay untyped
   dicts (tiering rules in `base/telemetry/audit_events.py`).

---

## 7. Inventory method and coverage (reproducible)

1. **Registry**: the `EVENTS` in `base/events/contract.py`, merged from
   `base/events/declarations/`, is the single source of truth; this document is generated by `scripts/codegen/gen_event_registry.py` (the
   pre-commit `events-registry-fresh` hook verifies no drift).
2. **Code-literal cross-check**: `python base/events/scan_kinds.py` — scans
   production Python code (excluding tests/worktrees/node_modules) for `event=`,
   `event_type=`, SSE roles, emits three inventories, and cross-checks them
   bidirectionally against the registry (`tests/contracts/test_lint_event_kinds.py`).
3. **Coverage statement**: this document covers every registered event_name (audit +
   telemetry + log + destination=file + SSE role). Not covered: test-fixture
   self-made names like `evt`/`my_event`/`some_warning` (non-production events).

**CI wiring**: `tests/contracts/test_lint_event_kinds.py` is a three-way guard — every static
`event=` literal in code must be registered (in `EVENTS`), every registry entry must
have a producer (event=/SQL/dynamic inventories), and this document must match
the generator output (drift = red). Emit-side fail-fast additionally raises
`ValueError` for unregistered names.

**When to regenerate**: run the generator before any PR that adds/renames an
event_name or SSE role; the pre-commit hook blocks drift.
"""


def _row(name: str) -> str:
    """One registry table row for `name`."""
    spec = EVENTS[name]
    keys = ", ".join(payload_keys(name)) if payload_keys(name) else "—"
    dest = "file" if spec.destination == "file" else "events"
    return f"| `{name}` | {spec.doc} | {spec.tier} | {keys} | {dest} |"


def _row_telemetry(name: str) -> str:
    spec = EVENTS[name]
    keys = ", ".join(payload_keys(name)) if payload_keys(name) else "—"
    dest = "file" if spec.destination == "file" else "events"
    fam = spec.family or "—"
    stored = "✓" if spec.persist else "—"
    return f"| `{name}` | {spec.doc} | {spec.tier} | {keys} | {fam} | {dest} | {stored} |"


def render() -> str:
    audit = [n for n, s in EVENTS.items() if s.category == "audit"]
    telemetry = [n for n, s in EVENTS.items() if s.category == "telemetry"]
    log = [n for n, s in EVENTS.items() if s.category == "log"]

    out = _HEADER

    out += _AUDIT_INTRO
    for name in audit:
        out += _row(name) + "\n"

    out += _TELEMETRY_INTRO
    for name in telemetry:
        out += _row_telemetry(name) + "\n"

    out += _LOG_INTRO
    for name in log:
        out += _row(name) + "\n"

    out += _SSE_INTRO
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
                "ERROR: base/events/registry.md is out of sync with the registry.\n"
                "   run .venv/bin/python scripts/codegen/gen_event_registry.py to regenerate"
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
