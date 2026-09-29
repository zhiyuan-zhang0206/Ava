"""The unit `.env` boot chain: the env-key registry and host-port block, the
boot-lite static config index, the single `.env` load, idempotent `.env` writes
and their audit trail, runtime config access, and the gateway bootstrap fetch.

Members: `registry` (env keys and their projections), `port_block` (the one
service -> port-offset table), `config_lite_table` (+ the generated
`config_lite_table.json` beside it), `dotenv_boot` (the single `.env` load every
process entry point imports), `dotenv_file` (locked, idempotent `.env` edits),
`audit` (owner-only write history), `runtime_config` (the unit's `.env` accessor)
and `bootstrap` (cluster-common config fetched from the gateway at start).

Docstring-only door: this chain runs before Settings exists, on paths that must
not load `shared.config`, so importing the package pulls nothing — import the
member module you need (`from shared.host.env import dotenv_boot`).
"""
