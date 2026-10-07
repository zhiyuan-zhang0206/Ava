# `ava.self.update()` removed — the CLI is the only update entry point

## Context

`ava.self.update()` let an agent trigger a whole-cluster rollout from inside its own turn, while the CLI's detached `ava-rollout` session had no such constraint. The two initiator paths behaved differently mid-rollout: an SDK initiator that missed the quiesce signal (the 2026-07-13 wait-for-quiesce design, superseded by this entry) rode out the rollout on old code for rounds — an agent-initiated update could not be made to behave like the CLI's, because the initiator is inside the thing being updated.

## Decision

Per user ruling: **remove the SDK path entirely.** The CLI `ava cluster update` is the only update entry point; `ava.self.update()` now raises `RuntimeError` pointing at the CLI. The gateway's rollout endpoint is still the trigger for the frontend "Update" button, which spawns the same detached `ava-rollout` session.

Alongside the removal, implement the user-specced **two-mode agent drain**:

- **smooth** (default): quiesce wait = `exec_timeout_seconds` × 1.2 (360s), so a healthy agent's single `execute_code` is guaranteed to end at its turn boundary; stragglers are force-reaped (CAS-marked restarting, process killed) and respawned by the restarter on new code.
- **force**: ~10s drain, then force-reap whoever is still live.
- The watchdog self-heal (`spawn_update` with `mode != 'none'`) signals this host's live agents, waits per mode, and force-reaps on timeout — the same drain contract as a rollout, instead of bouncing services under running agents. A rollout's Phase B passes `mode='none'` (the gateway-side quiesce already drained the fleet) plus `force_reap` when the quiesce timed out on stragglers.

## Alternatives rejected

- *Keep `ava.self.update()` and make it block like the CLI.* The initiator is the thing being updated — no in-process wait can make its own replacement atomic; the quiesce-wait design (07-13) still had the ride-out failure mode.
- *Restrict `ava.self.update()` to non-backend rollouts.* The SDK cannot reliably know whether the rollout will change code under it, and the check would drift from the orchestrator's actual decision.

## Consequences

- Updates are operator/frontend-initiated only; an agent cannot roll the cluster itself (agents wanting new code wait for the next rollout or `ava restart`).
- The two-mode drain gives rollouts a bounded, honest shutdown: healthy agents finish at their turn boundary; wedged ones are force-reaped rather than leaving the host on mixed code.
- Historical `self:update` rows and checkpoints are still tolerated defensively by the claim/state/db layers.
