---
title: Agent list machine visibility
---

# Agent list machine visibility

`ava agents ls` now serves as an external-context bridge as well as a compact
operator view. An agent id and lifecycle state do not identify where work is
running in a multi-runner cluster; the machine is the minimum additional fact
an external operator or coding agent needs to interpret the roster and choose
the correct target. Runner workspace paths are not a substitute because they
are runner-local and cannot be made meaningful on the gateway host.

The command therefore requests the existing authenticated
`GET /api/agents?fields=summary` projection and renders only `agent_id`,
`status`, `machine`, and `label`. Reusing the shared summary boundary keeps the
external-context read aligned with the roster contract instead of extending the
CLI-only compact projection into a second context API.

This reverses the [compact-projection decision](../2026-09-01/agents-list-compact-projection.md).
The measured 2026-09-01 sample was 381,791 compact bytes versus 2,460,642
summary bytes for 5,218 rows: the summary response is about 6.4 times larger
and transfers 2,078,851 additional bytes for that sample. It also performs the
summary projection's roster lookups rather than the compact query's three-column
join. We accept that cost because `ava agents ls` is an on-demand context read,
not a polling roster surface, and machine visibility is required for the result
to be operationally complete. High-frequency consumers should continue using
their purpose-built scoped projections rather than polling this all-history CLI
command.

`fields=compact` remains available as a legacy narrow gateway projection. This
decision changes the CLI consumer; it does not remove the endpoint or authorize
the gateway to fabricate absolute workspace paths.
