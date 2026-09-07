"""The port-block layout — one service -> offset table, shared by every consumer.

A unit's listening ports come from a contiguous block: the block's base plus the
service's offset. Two consumers need that table and they sit on opposite sides of
the config import:

- `shared.cluster` allocates a block per cluster at install time and writes the
  resulting ports into that cluster's `.env` (`derive_env`). It imports
  `shared.config`.
- `cli.enroll` derives one unit's daemon health ports from an operator-supplied
  base (`--health-port-base`) and must import NEITHER `shared.config` nor
  `cli.commands` — it runs on a host that has no full config yet.

So the table lives here, in a module that imports nothing, rather than being
restated in the settings-free half and guarded by an equality test. A second copy
of these numbers is a silent-drift source: the offsets are invisible in a `.env`,
so a unit whose ports disagree with its own registry record looks fine until
something probes the wrong port.
"""

from __future__ import annotations

PORT_OFFSETS: dict[str, int] = {
    "gateway": 0,
    "frontend": 1,
    "heartbeat": 2,
    # Retired service slot: reserved for existing home records; never reused.
    "restarter": 3,
    "labeler": 4,
    "task_maintenance": 5,
    "memory_indexer": 6,
    "ops": 7,
    "milvus": 8,
    "browser": 9,
    "permissions_helper": 10,
    # Per-cluster Postgres + Redis: every cluster runs its OWN instance on its own
    # port (never a shared 5432/6379) so two co-located clusters cannot reach each
    # other's data plane. Every registry record carries these ports.
    "postgres": 11,
    "redis": 12,
    # PgBouncer transaction pooler in front of this cluster's Postgres (the pooled
    # front door consumers dial past ~50 agents). On by default (AVA_PGBOUNCER_ENABLED
    # is a kill-switch, see shared/config/data_plane.py); the slot is reserved in every
    # block regardless so toggling it never needs a re-alloc. The pooler port is a
    # REGISTRY fact only — it is never materialized into .env (AVA_DB_URL carries
    # the pooler port when pooling is enabled).
    "pgbouncer": 13,
    "events_maintenance": 14,
    # The Next.js app the gate proxies to — a separate slot because the entry
    # port (offset 1, "frontend") is owned by the always-up gate.
    "app": 15,
    # The IM bridge + delivery watchdog — the two daemons added AFTER the
    # per-unit health-port decision (decisions/2026-07-31-a-health-port-
    # belongs-to-a-unit.md) landed. They were given static ports (8111/8110)
    # and fell out of the per-unit model: `--health-port-base` did not move
    # them, so a second co-located unit collided on the shared default exactly
    # like the 2026-07-26 incident the decision fixed (F-s4-2). They get the
    # two slots past `app`; BLOCK_SIZE grows with them (16 -> 18), and
    # `allocate_ports` must then skip candidate bases that would overlap a
    # pre-existing 16-port block (see there).
    "delivery_watchdog": 16,
    "im_bridge": 17,
    "page_server": 18,
    # The hosted agent-runner (future/infra/agent-runner-as-server.md). Appended
    # rather than inserted, for the same reason delivery_watchdog/im_bridge were:
    # an existing cluster's registry record pins its block base, so renumbering a
    # live offset would move a running daemon's port out from under it.
    "agent_host": 19,
    # Offset 20 is deliberately VACATED: the idle-shell-reminder daemon held it
    # until its removal (2026-08-27) and renumbering pg_backup down into the
    # hole would move a live daemon's port on every existing cluster (records
    # pin their block base; a missing-key derive must agree with the .env it
    # predates). BLOCK_SIZE stays 22 so the block still covers pg_backup.
    # The backup scheduler is a first-class health daemon. Append its slot so
    # existing cluster records retain their assigned ports.
    "pg_backup": 21,
    "pitr_uploader": 22,
    "pitr_base_backup": 23,
    # The memory search service (numpy backend) — appended like every late
    # slot: existing records pin their block base, so renumbering a live
    # offset would move a running daemon's port out from under it.
    "memory_search": 24,
    # The two capability watchdogs now expose their own health endpoints.
    # They must occupy distinct slots because a single-box unit runs both.
    "gateway_watchdog": 25,
    "agent_runner_watchdog": 26,
}
BLOCK_SIZE = 27
BLOCK_START = 18000
BLOCK_MAX = 20000


# The default home's (prod ~/.ava) FIXED service ports — the pre-registry legacy
# block. Lives here, dependency-free, so the settings-free installed-home gate
# (cli/preflight.py) can derive a record's pgbouncer port without importing
# shared.cluster (which imports shared.config).
#
# ADDING A SLOT: this table is NOT in offset order and never has been — `ops`
# sits at 8113 because it moved off 8106 to dodge a Windows service that
# permanently holds that port. So "the next number after the last line" is a
# booby trap: it produced 8113 for `agent_host`, which `ops` already held, and
# the whole suite passed because nothing checked this table for duplicates.
# These are the ports a unit whose `.env` predates a key ACTUALLY BINDS
# (`daemon_health.DEFAULT_PORTS` is derived from here), so a duplicate is two
# daemons fighting over one socket on every existing unit — and that collision
# would have taken the ops server's port, which the gateway dials for every
# runner RPC. Pick a number no other entry holds; `test_legacy_ports_are_unique`
# now fails the run if you don't.
LEGACY_AVA_PORTS: dict[str, int] = {
    "gateway": 8000,
    "frontend": 3000,
    "app": 3001,
    "heartbeat": 8107,
    # Historical registry data only; no runtime service binds this port.
    "restarter": 8102,
    "labeler": 8103,
    "task_maintenance": 8108,
    "memory_indexer": 8105,
    # ops: NOT 8106 — the Windows iphlpsvc service (svchost) permanently holds
    # 8106 on Windows hosts, so a default there makes ops fail to bind on every
    # Windows unit (2026-08-11 incident, task #1179; win overrides to 8107).
    "ops": 8113,
    "milvus": 19530,
    "browser": 9222,
    "permissions_helper": 9223,
    "postgres": 5433,
    "redis": 6380,
    "pgbouncer": 6433,
    "events_maintenance": 8109,
    "delivery_watchdog": 8110,
    "im_bridge": 8111,
    "page_server": 8112,
    # 8114, NOT 8113: `ops` already holds 8113 (it moved off 8106 to dodge the
    # Windows iphlpsvc grab, which is why this table is not in offset order).
    # These are the ports a unit whose `.env` predates a key actually binds, so
    # a duplicate here is not a cosmetic clash — the host would take the ops
    # server's port on every existing unit, and the ops server is what the
    # gateway dials for every runner RPC. `test_legacy_ports_are_unique` is the
    # guard.
    "agent_host": 8114,
    # 8115 was the idle-shell-reminder's fixed port (daemon removed 2026-08-27);
    # left unused rather than renumbered, for the same record-pinning reason as
    # the vacated block offset above.
    "pg_backup": 8116,
    "pitr_uploader": 8117,
    "pitr_base_backup": 8118,
    "gateway_watchdog": 8119,
    "agent_runner_watchdog": 8120,
    # The memory search service's TCP port (like milvus's 19530, not a health
    # port — its healthcheck probes the real /search endpoint).
    "memory_search": 19531,
}
