---
type: doc
title: External SDK Attachment
description: Local external Python processes borrow an approved agent identity and journal plugin state while the native graph owns checkpoints.
tags:
- sdk
- agents
---

# External SDK Attachment

`ava.external.attach(lease_id, token=...)` binds one active, same-machine controller
lease to a Python process. The external model keeps its own tools; SDK calls execute
in that external process. The attachment is a context manager with explicit `flush`
and `close` methods, and never starts or renews a lease.

`ava._boot.validate_external_identity` checks the lease and state version on SDK
identity paths, including provenance and MCP requests. `PluginStateHandle.read` and
`update` perform the same check; raw `ava.state` remains a local snapshot rather
than a lease-aware proxy. Native runtime identity paths remain unchanged
when no external attachment exists. The attachment temporarily overrides an
external caller profile so peer operations carry `agent:N`, then restores the
ordinary profile when closed.

Plugins load through the existing extension loader. Framework and plugin config
views bind the agent's stored configuration, while `_external_state.load_snapshot`
reads its checkpoint without writing it. Pending journal entries are replayed
through their registered reducers after the checkpoint's applied receipt.

Plugin updates use the checkpoint serializer and its registered type allowlist,
encoded into JSON envelopes without coercing reducer inputs. Flushing appends one
ordered delta under a lease version comparison. The native graph alone checkpoints
that journal and records an applied receipt, preventing repeat application after
a crash between checkpoint persistence and database acknowledgment.

The usage procedure and CLI commands live in
[External agent impersonation](../conventions/agent-impersonation.md).
