# The update straggler reap is removed

## Context

[The 2026-09-19 ruling](2026-09-19-straggler-reap-in-update-waves.md) let an
update wave truncate and release a cohort member still un-landed a few seconds
after its restart command, so one long turn could not stall a fifty-agent wave.
The kill was a durable mark: the drain CAS-marked the agent's row `restarting`,
the in-flight turn read the mark as an abort signal, and a successor boundary
(the agent-host boot or the local resume) settled the mark and woke the agent.
The mechanism reached into the hold journal (`reaped` receipts), the drain
certification, the interrupt read on the claim hot path, the agent-host turn
classifier, the boot settle, four telemetry events and two settings.

The drain enabled it only through `drain(reap=True)`. Its callers were the
lease-bound cluster-stop op (through `pause_local_cluster`, gone with the
unified lifecycle) and the release drain and the PITR activation transition
(directly), which went with the
[release/image update path](2026-09-30-remove-release-image-path.md). A cluster
is now updated by `python -m cli.fleet_update`, whose stop is
`ava stop -y --timeout N`: a member in a long turn holds the stop until the
timeout, the stop fails with its hold retained, and the operator retries or
escalates with `--force` on site, as the
[source-mode decision](2026-09-30-networked-cluster-stays-on-source-updates.md)
accepts. That path never reaped, so nothing could trigger the code.
[Postmortem 0009](../postmortems/0009-complexity-must-name-the-failure-it-prevents.md)
states the rule it fails: complexity must name the failure it prevents, and this
one prevented a stall in a fleet-wide automatic wave that no longer exists.

## Decision

Delete the reap as a whole, without a compatibility layer:

- the drain's reap branch (`drain(reap=...)`, `pause_agents(reap=...)`, the
  window probe, the CAS mark) and `pause_local_cluster`, which existed only to
  call it, with `update_straggler_reap_seconds` and
  `update_quiesce_timeout_seconds`, the latter read only by that entry;
- `MaintenanceHold.reaped`, `unsettled_failures` (every gate reads `failures`
  again), `admission.record_reaped` and the reap certification in
  `cohort.verify_drained`;
- the settle module (`straggler_reap`), its call at the local unpause and at the
  agent-host boot, and the host's paced first-admission wake for settled rows;
- the agent-host truncation classifier for the mark, keeping the classifier for
  an applied force terminate;
- the reap branch of the interrupt read (`agent.db.pending_interrupt_reason`
  fires on cancel and terminate only);
- the events `update_straggler_reaped`, `update_straggler_reap_settled`,
  `host_turn_truncated` and `host_held_wake_truncated`.

Nothing writes `agents_meta.status = 'restarting'` any more. The value stays in
`AgentStatus` and the column's CHECK, and the readers that classify an old row
(the legacy cold normalization, the host's unrunnable set, the heartbeat
projection) stay; the schema and enum are cleaned up in their own step.

## Alternatives rejected

- **Keep it dormant for a later update path.** It costs every lifecycle change a
  second set of receipts and a hot-path SQL branch to keep compiling and proving,
  for a trigger no operator has. Git history keeps it, and a later update path
  starts from a design against its own need.
- **Give `ava stop` a reap.** It would add an automatic kill to an attended
  stop whose failure the operator handles on site, and reintroduce at-least-once
  replay of a turn's side effects that the operator can judge case by case.
- **Drop the `restarting` status now.** It is a schema and API-contract change
  (the column CHECK, the enum, the generated API types, the UI) and belongs to
  the step that changes the schema.

## Consequences

- `ava stop`, `ava pause` and the fleet update stop behave as before: a long
  turn holds the drain until its deadline, the hold is retained, and an operator
  decides.
- A hold journal written by the previous code still reads: its `reaped` map is
  ignored and no longer written.
- `AVA_UPDATE_STRAGGLER_REAP_SECONDS` and `AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS`
  are unknown to the settings; a stale line for either in a `.env` is inert.
- A row left in `restarting` by an earlier reap is no longer settled at boot or
  resume. None is expected (a mark was settled at the next agent-host boot or
  local resume of its unit, and nothing has reaped since the update path
  changed), and one that exists is repaired by hand.
- The two decisions of 2026-09-19 and 2026-09-20 about the reap stay as the
  record of what it was.
