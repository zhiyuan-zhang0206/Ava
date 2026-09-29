"""Fleet release transition: one coordinator decides; units follow.

The coordinator (`coordinator.py`) is the gateway home's finite executor; it
drives the fleet journal through its phases, performs the gateway unit's own
effects (`gateway.py`) and instructs remote units over the authenticated
coordinator channel (`units.py`, `listener.py`). A remote unit's executor
follows those instructions (`follower.py`, `client.py`). A single box is a
fleet of one. The workload policy (`policy.py`, `workload.py`, `alerting.py`,
`publication.py`) is pure; the coordinator journals each of its results
before executing it.
"""
