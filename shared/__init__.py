"""UI <-> kernel shared boundary layer.

`ui/*.py` is not allowed to import `agent/.*` — the two sides only
couple through DB (Postgres) + Redis pub/sub. This package centralizes
**pure helpers used by both ends**:

- `config`: process config (settings singleton)
- `db`: pure SQL helpers (no business semantics, both UI/kernel call)
- `events`: Redis pub/sub event schema (pydantic discriminated union)
- `exit_codes`: subprocess exit-code constants

Kernel-only helpers (inbound claim, wait/mark/revert etc.) live in
`agent/db.py` because they entangle with kernel-only state-machine
logic. Postgres DDL is not in the Python package — see
`db/schema.sql`.
"""
