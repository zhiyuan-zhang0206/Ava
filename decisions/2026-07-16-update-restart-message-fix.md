# update/restart Audit and Fix for Restart Message Loss

> **Status 2026-08** — written for the removed `ava.self.update()` initiator
> path (`self:update` restart rows). The restart-completed source-priority SQL
> it produced (`system:update` > `self:update`) still lives in
> `ops/agent_wake.py` and still matters for historical rows; no new
> `self:update` rows are written.

## Problem
After `ava.self.update()`, the agent process is restarted, but the restart completion message does not reach the agent, causing the agent to not know that it has been restarted.

## Root Cause Analysis

### Trace

The complete call chain of `ava.self.update()`:

```
SDK: ava/self.py:update()
  → POST /api/cluster/rollout (gateway)
  → spawns the rollout session
  → runs ava update --local:
    1. Phase A: Pause restarters
    2. Quiesce: Insert system:update restart inbound for all live agents
    3. SDK poll detects system:update → inserts self:update restart + raises AgentRestart
    4. Gateway: stop → pull → sync → start
    5. Phase B: fan-out agent-runner updates
  → SDK raise AgentRestart:
    → exec node: halted=True, goto after_exec
    → claim node: process restart batch
      - process system:update restart: status→RESTARTING
      - process self:update restart: update_initiated=True, restart_preserves_idle=False
      - Write state to checkpoint: halted=False, update_initiated=True
      - goto END
  → Process exits
  → Restarter: detects RESTARTING → respawn_agent():
    1. UPDATE RESTARTING → ALLOCATED
    2. Find the nearest restart inbound → **BUG: prioritizes self:update, should prioritize system:update**
    3. INSERT restart_completed (source=self:update)
    4. Start new process
  → New process: claim node processes restart_completed
    - source=self:update → marker: "update is rolling out" (incorrect marker)
    - update_initiated not cleared (source != system:update)
    - goto before_llm → agent is woken up but sees incorrect message
```

### Bug 1: Incorrect source priority in respawn_agent()

When `ops/agents.py:respawn_agent()` selects the source for restart_completed, it uses:
```sql
ORDER BY id DESC LIMIT 1
```
When both `system:update` and `self:update` exist, `self:update` has a higher id (inserted later), causing the source of `restart_completed` to be `self:update` instead of `system:update`.

**Impact**: The agent wakes up and sees "update is now rolling out" instead of "updated and restarted".
The `update_initiated` flag also cannot be correctly cleared (only cleared when source=system:update).

### Bug 2: Silent loss on checkpoint write failure

After the old process's claim node finishes processing the restart batch, LangGraph writes `halted=False, update_initiated=True` to the checkpoint. If this write fails (process killed / checkpoint write race), the new process loads the old state (`halted=True, update_initiated=False`).

At this point, the claim node processes `restart_completed`:
- `state.halted=True` + batch only has restart_completed → triggers idle-restart gate
- returns goto=CLAIM (back to waiting), agent never sees the restart marker
- **Agent is restarted but never informed**

This scenario is not easily triggered in the normal path of `ava.self.update()` (clean exit), but may occur in the following situations:
- Process is SIGTERMed during checkpoint write
- DB connection drops during write
- Any exception preventing graph.ainvoke from returning normally

## Fix

### Fix 1: respawn_agent() source priority
`ops/agents.py`: Change SQL query to use `CASE` expression, prioritizing `system:update` > `self:update` > others. Ensure `restart_completed` uses the correct source, generating the correct "updated and restarted" marker.

### Fix 2: Stale-state recovery in claim node
`agent/graph/_claim.py`:
- RESTART_COMPLETED handler: When source=`self:update` and `update_initiated=False`, re-set `update_initiated=True` (stale-state recovery). Only effective for `self:update` (unique marker for initiator), does not affect `system:update` (can be sent to any idle agent).
- Idle-restart gate: Add `not update_initiated` condition to ensure the agent is woken when `update_initiated=True`.

## Modified Files
- `ops/agents.py` (+13/-5): respawn_agent() source priority
- `agent/graph/_claim.py` (+30/-5): RESTART_COMPLETED handler + idle gate

Forward link (2026-08-22): respawn now returns a row to unclaimed idling; see
[agent status model](../docs/history/2026-08-22/agent-status-model.md).
