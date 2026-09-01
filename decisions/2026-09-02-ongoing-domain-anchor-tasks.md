# Ongoing status for domain-anchor resident tasks

## Context

The 2026-08-27 root-only ruling reserved `ongoing` for the system tree anchor. Task #2165 established that long-running domain-anchor resident tasks need the same reminder-exempt active state. QA adversarial review of PR #1354 identified the remaining root-only assumptions in presentation and machine pause draining. Task #405 sign-off authorizes the operational boundary and ownership control.

## Decision

- Regular domain-anchor resident tasks may use `ongoing` for long-running active work. The system root remains permanently `ongoing` and immutable.
- The SDK permits a real change to `ongoing` only from the task owner or an ancestor of that owner in the agent spawn chain. Calls with no agent identity remain available to system tooling.
- Gateway PATCH remains ungated because it is the human/user control channel, not an agent peer operation.
- An owner or delegator may return an ongoing task to `in_progress`; `done` and `cancelled` close it. Ongoing counts as active when a parent is closed.
- PMO cleanup treats ongoing as active. Machine pause drains ongoing tasks with other active work, and the SDK ownership gate governs agent-initiated use.

## Alternatives rejected

- **Keep ongoing root-only:** domain-anchor resident work would continue to receive reminders despite being deliberately long-running.
- **Let every SDK peer set ongoing:** a peer could suppress reminders on another agent's work without that owner's delegation.
- **Apply the SDK gate to gateway PATCH:** this would restrict the human control channel with agent-lineage data that does not represent its authority.

## Consequences

The active-task definition now consistently includes both `in_progress` and `ongoing` where work must remain owned, drained, or block parent closure. The partial title uniqueness invariant remains intentionally scoped to `in_progress`; ongoing residents may share titles.
