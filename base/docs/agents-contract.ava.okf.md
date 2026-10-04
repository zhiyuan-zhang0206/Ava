---
type: doc
title: Agent Cross-Process Contract
description: '`base/agents/contract.py` defines the cross-process data contract between agent processes and the gateway — pure type definitions, no implementation. Both sides communicate over HTTP and must see the same status enums, exception hierarchy, and wire error protocol (bidirectional mapping between wire reason ↔ exception classes). The message-level half of the contract is its sibling `base/agents/messages/kwargs.py`.'
tags:
- base
- library
- agent-lifecycle
---

# Agent Cross-Process Contract

## What it is

`base/agents/contract.py` — the cross-process data contract between agent processes and the gateway: pure type definitions, no implementation. Both sides communicate over HTTP and must share the same status enums, exception types, and wire error protocol.

## Core responsibilities

### Status and result enums
- `AgentStatus` (StrEnum) lifecycle states: `RUNNING` (claimed process, including boot) → `IDLING` (waiting for wakeup between turns or unclaimed before boot) → `TERMINATED`.
- `TerminationSource` (StrEnum) — who wrote `status='terminated'`: `USER`/`EXIT`/`REAPER`/`LAUNCH_CONFIRM`/`INTEGRITY`, stamped by EVERY terminated-write in the same statement (NULL is permanently unresurrectable — `scripts/lint/termination_source.py` enforces it). Historical termination-source values remain readable after retiring per-agent process supervision. There is no closed state: a terminated agent may be resurrected by any new message ([decision](../../decisions/2026-09-27-terminate-has-no-closed-state.md)).
- Operation result enums: `TerminateResult` / `RestartResult` / `ResurrectResult` — encode idempotent operation outcomes (enqueued / already_terminated / already_alive …) as wire strings.

### Wire error protocol
- `ErrorReason` (StrEnum) is the error identifier on the SDK ↔ gateway HTTP wire. `agent_launch_failed` is the post-commit spawn case: the envelope and SDK exception preserve the committed `agent_id`, current state, and ID-based retry path.
- Gateway side: catches `AvaAgentError` subclasses → response body `{"detail": str(exc), "reason": exc.reason}` + `exc.http_status`.
- SDK side: parses the response `reason` → looks up `EXCEPTION_BY_REASON` to reconstruct the same exception type and throw to caller (preserving the original message).
- `AvaAgentError.__init_subclass__` enforces both `reason`/`http_status` ClassVars (missing → `TypeError` at import). `EXCEPTION_BY_REASON` is a literal table at the module end. New error = enum value + exception class + one table row.
- End-of-module check (`raise`, not `assert`, so it survives `-O`) requires the table to cover every `ErrorReason` and every `AvaAgentError` subclass, each under its own `reason` — closing the "added enum, forgot class" and "added class, forgot row" gaps.
- `gateway/middleware/tests/test_agent_error_wire_equivalence.py` parameterizes `EXCEPTION_BY_REASON.values()` to lock down end-to-end roundtrips.

## Key dependencies

- [[kwargs.ava.okf.md]] — the sibling contract module: the message-level half, typing the `ava_*` metadata inside a message's `additional_kwargs` where this module types the HTTP wire between the two processes
- [[gateway-cli.ava.okf.md]] — spawn/respawn/launch/fork/resurrect implementations live behind `ops/agents/__init__.py` (`ops/agents/spawn.py` birth + `ops/agents/wake.py` wake); this module provides only types
- [[agent/docs/lifecycle.ava.okf.md]] — the host applies native lifecycle commands under exact-incarnation ownership.

## Entry points

- `base/agents/contract.py:AgentStatus.RUNNING` — status enum value
- `base/agents/contract.py:AgentNotFound` — exception class (http_status=404, reason="agent_not_found")
- `base/agents/contract.py:EXCEPTION_BY_REASON` — reverse lookup table from wire reason → exception class

## Notes

- **Not all exceptions go over the wire**: `GatewayUnavailable` (SDK-only, no response) and `ResurrectAlreadyAlive` (locally an idempotent 200 `already_alive`) are **not** in `EXCEPTION_BY_REASON`; `ResurrectError`/`ForkError` are marker base classes for catch grouping, no wire fields.
