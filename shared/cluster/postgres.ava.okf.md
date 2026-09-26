---
type: doc
title: Owned PostgreSQL birth
description: Durable native custody for directly launched home-owned PostgreSQL.
---

# Owned PostgreSQL birth

`cli.commands._cluster_instance` launches the home's `postgres` directly in
its own POSIX session. The data plane survives application-root replacement.
It does not use `pg_ctl` to detach, discover, reload or stop the home server.
Throwaway restore/test PostgreSQL has a separate retained-process boundary.

`$AVA_HOME/run/postgres.json` records pending admission before spawn, then the
captured native child before readiness. Linux identity is boot UUID, PID and
mandatory kernel start ticks; macOS uses boot UUID and the exact stable kernel
birth. The receipt also binds the canonical data path, directory device/inode,
port, and the ready server's pidfile device/inode and fixed header. Fresh and
retained starts share one locked, bounded protocol-readiness completion. A warm
start names its exact expected birth, reloads it, and admits a captured receipt
only after native process, protocol, pidfile and listener checks pass. An
unresponsive process cannot pass solely because its listener still exists, and
a changed or vanished expected birth is never replaced by warm start. The
pidfile's wall timestamp is file data, never a native birth comparison.

`shared.cluster.ownership.postgres` reads this evidence. Provisioning, admin
connections, ordinary stop and PITR consume the same owner. Missing receipts do
not adopt running or pidfile-recorded servers. An interrupted pending launch
without a captured birth requires explicit reconciliation. SIGINT/SIGTERM are
deferred across child creation and receipt publication with Python handlers;
children inherit ordinary signal masks. A crash still retains ambiguous
admission instead of inventing closure.

Fast shutdown sends SIGINT through the exact native process boundary, waits
for captured descendants, then verifies no PostgreSQL process for the retained
data directory/inode, no process in the retained session, and no listener.
Linux signals use a PID-retaining descriptor. Reload uses the same receipt and
native identity gate. Unknown metadata, surviving archive children or a replaced
pidfile refuse. Clean stop retains the receipt; a subsequent launch replaces it
only after positive native closure. PITR can then swap PGDATA and the next
launch records the new directory identity. No clock tolerance or automatic
legacy adoption exists; old unrecorded installations require operator cutover.

## Admin authority

`shared.pg_admin` builds every DDL-capable dial to this server. The OS user
(the initdb bootstrap superuser) connects over the home's owner-only socket
and binds the backend to the postmaster above. It acts as itself for roles,
databases, extensions and grants. Object creation goes through
`owner_session`/`OwnerAuthority` instead, with `role=<owner>` as a startup
option: the baseline, checkpoint setup, migrations and the pgvector memory
table stay owner-owned and see only the owner's privileges. The session role
survives transaction rollback and `RESET ROLE`, and is verified before use.
The owner therefore never has to log in, which is what lets it lose `LOGIN`
later. `OwnerAuthority.conninfo` gives password-free libpq tools such as
`pg_dump` the same owner-equivalent view.
