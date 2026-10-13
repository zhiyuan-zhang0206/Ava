"""Agent birth: the row-creation transaction, its keyed-creation identity and the spawn entry.

`spawn` creates a new agent row (optionally forked from another agent's checkpoint),
`transaction` is the single DB transaction behind it, and `creation_identity` holds the
immutable keyed-creation snapshot a replayed request is matched against. Everything here
depends on `base` only: the gateway creates the row, and the runner's launch op in
`ops.lifecycle` validates it and wakes its host.
"""
