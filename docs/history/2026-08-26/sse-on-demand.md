# On-demand agent SSE

The conversation view should not receive every high-frequency event for every
agent when it observes only one agent. `/api/system/all` therefore accepts an
optional agent-id filter, while system-level `agent_id=0` signals remain visible
to filtered clients. Omitting the filter preserves the original broadcast
contract for callers that need it.

The frontend keys its conversation EventSource to the active agent. Background
tabs close that high-frequency connection and reconcile timeline, token usage,
and pending-message snapshots every seven seconds instead. Returning to a
visible tab reopens SSE and performs the normal open-time reconciliation. The
default batch flush ceiling is ten pushes per second.
