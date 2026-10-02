"""The turn boundary between the hosted runner and the graph: one graph
invocation is one turn.

Package door — no imports, no re-exports; callers import the public modules:

  - `runloop.py`          — the invocation config for one turn (`graph_config`),
    recoverable turn-error reporting, and fatal-turn settlement (circuit state,
    the permanent-reject streak, the ancestor report).
  - `trace_checkpoint.py` — after a turn commits, link its trace and its
    checkpoint in both directions.
  - `progress.py`         — the in-process per-agent turn-progress clock the
    hosted stall guard and dispatcher read, plus the admission-wait registry.
    A dependency-free leaf: the graph nodes mark it, `services/agent_host`
    reads it.

The hosted runner (`services/agent_host`) drives all three; see the
`services must not import the agent kernel` contract in pyproject.toml.
"""
