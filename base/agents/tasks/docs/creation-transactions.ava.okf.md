---
type: doc
title: Creation transaction primitives
description: Caller-owned task and agent-birth transactions with unchanged public wrappers
---

# Creation transaction primitives

`base.agents.tasks.creation.create_task_in_transaction` accepts an existing
cursor. It resolves the canonical priority/reminder policy, locks and validates
the parent, inserts the task and creation audit, and queues the established owner
notification policy in that transaction. It returns a `Task` and prepared audit
events. It never opens another connection, commits, wakes an agent or publishes
a task-board event. The caller owns rollback and post-commit emission.

`base.agents.tasks.model` owns the standard `Task` dataclass, its derived SQL
column order, timestamp rendering and complete immutable-snapshot validation.
The fleet SDK reexports the same class and retains its existing private column
and row-conversion aliases. Priority and reminder defaults/validation are owned
by `base.agents.tasks.priority`; no parallel SDK policy is introduced.

`ops.agents.birth_transaction.insert_agent_birth` similarly accepts the
caller's cursor. It writes the identity, metadata, optional fork history,
initial prompt and birth audit, returning an `AgentBirth` with prepared events.
It preserves the existing keyed-birth recovery owner: replay returns the
original launch attempt and no new prompt id or events. Birth remains a gateway
MAIN-role operation; the runner role cannot create identities.

The existing public `ops.agents.spawn.create_agent_row` wrapper still owns its
connection, commit, announcements, logging and legacy tuple result. Standalone
SDK task creation still owns its receipt advisory lock, validation, transaction
and post-commit effects. Receipt lookup precedes mutable parent/title checks;
`None` reminder intent is retained in the receipt before first-policy resolution.
Gateway code can use these native primitives without importing a plugin or SDK
actor context. Metadata actors describe provenance, not a new security ACL.

These primitives allow one caller transaction to roll back birth, task, audit
and inbound together. They do not make the existing `create_and_assign` recipe
atomic, introduce a compound endpoint, or guarantee launch/execution recovery.
A future compound acceptance owner must define its verified-principal scope,
frozen request, retained original result and post-commit launch separately.
