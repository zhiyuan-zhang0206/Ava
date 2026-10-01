"""Native data-plane instance tooling: binary location, foreground postmasters,
throwaway clusters, and the vendored relocatable distribution.

``pg_tools`` locates native Postgres binaries across the supported host
platforms and drives the throwaway-cluster lifecycle for tests; ``pg_runtime``
provisions the same installation runtime resolution selects; ``runtime_binaries``
fetches and vendors a relocatable Postgres + pgvector under `~/.ava/runtime/` so
a clean machine needs no brew/apt install. ``pg_foreground`` keeps a cancellable
worker's postmaster in its own process group rather than pg_ctl's daemonized
default, so it can be closed by group rather than left to escape untracked.
``pg_throwaway_base`` and ``pg_stall_watchdog`` are extracted from `pg_tools` to
keep it under its line ceiling: the former holds the throwaway data-directory
host facts and disk-headroom policy, the latter watches a throwaway cluster for
a stall a timed-out test suite would otherwise leave unexplained. ``pooler`` is the
PgBouncer observation half shared by the cli and the root diagnostics: where the
pooler's files live and whether its listeners answer.
"""
