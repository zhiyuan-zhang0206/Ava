---
type: doc
title: Agent Cross-Process Contract
description: '`shared/agents.py` defines the cross-process data contract between agent processes and the gateway — pure type definitions, no implementation. Both sides communicate over HTTP and must see the same status enums, exception hierarchy, and wire error protocol (bidirectional mapping between wire reason ↔ exception classes). The message-level half of the contract is its sibling `shared/message_kwargs.py`.'
tags:
- shared
- library
- agent-lifecycle
---

# Agent Cross-Process Contract

## What it is

`shared/agents.py` — the cross-process data contract between agent processes and the gateway: pure type definitions, no implementation. Both sides communicate over HTTP and must share the same status enums, exception types, and wire error protocol.

## Core responsibilities

### Status and result enums
- `AgentStatus` (StrEnum) lifecycle states: `RUNNING` (claimed process, including boot) → `IDLING` (waiting for wakeup between turns or unclaimed before boot) → `RESTARTING` (process replacement in progress) → `TERMINATED`.
- `TerminationSource` (StrEnum) — who wrote `status='terminated'`: `USER`/`EXIT`/`REAPER`/`LAUNCH_CONFIRM`/`INTEGRITY`, stamped by EVERY terminated-write in the same statement (NULL is permanently unresurrectable — `scripts/lint_termination_source.py` enforces it). Historical termination-source values remain readable after retiring per-agent process supervision.
- Operation result enums: `TerminateResult` / `RestartResult` / `ResurrectResult` — encode idempotent operation outcomes (enqueued / already_terminated / already_alive …) as wire strings.

### Wire error protocol
- `ErrorReason` (StrEnum) is the error identifier on the SDK ↔ gateway HTTP wire (currently 9 values: `agent_not_found` / `fork_source_empty` / `fork_checkpoint_not_found` / `machine_not_registered` / `spawn_target_not_agent_runner` / `cross_machine_gateway_unavailable` / `indexer_unavailable` / `channel_not_configured` / `invalid_model_config`).
- Gateway side: catches `AvaAgentError` subclasses → response body `{"detail": str(exc), "reason": exc.reason}` + `exc.http_status`.
- SDK side: parses the response `reason` → looks up `EXCEPTION_BY_REASON` to reconstruct the same exception type and throw to caller (preserving the original message).
- `AvaAgentError.__init_subclass__` auto-registers subclasses into `EXCEPTION_BY_REASON` at declaration and enforces both `reason`/`http_status` ClassVars (missing → `TypeError` at import)—the table is the single source of truth. New error = enum value + exception class; registration is automatic.
- End-of-module assertion `set(EXCEPTION_BY_REASON) == set(ErrorReason)` (`raise`, not `assert`, so it survives `-O`), closing the "added enum, forgot class" gap.
- `tests/test_agent_error_wire_equivalence.py` parameterizes `EXCEPTION_BY_REASON.values()` to lock down end-to-end roundtrips.

## Key dependencies

- [[message_kwargs.ava.okf.md]] — the sibling contract module: the message-level half, typing the `ava_*` metadata inside a message's `additional_kwargs` where this module types the HTTP wire between the two processes
- [[gateway-cli.ava.okf.md]] — spawn/respawn/launch/fork/resurrect implementations live behind `ops/agents.py` (`ops/agent_spawn.py` birth + `ops/agent_wake.py` wake); this module provides only types
- [[agent/lifecycle.ava.okf.md]] — the host applies native lifecycle commands under exact-incarnation ownership.

## Entry points

- `shared/agents.py:AgentStatus.RUNNING` — status enum value
- `shared/agents.py:AgentNotFound` — exception class (http_status=404, reason="agent_not_found")
- `shared/agents.py:EXCEPTION_BY_REASON` — reverse lookup table from wire reason → exception class

## Notes

- **Not all exceptions go over the wire**: `GatewayUnavailable` (SDK-only, no response) and `ResurrectAlreadyAlive` (locally an idempotent 200 `already_alive`) are **not** in `EXCEPTION_BY_REASON`; `ResurrectError`/`ForkError` are marker base classes for catch grouping, no wire fields.
