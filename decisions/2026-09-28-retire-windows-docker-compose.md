# Retire the Windows Docker Compose data plane

## Context

[`2026-06-18-windows-wsl2-docker-path.md`](2026-06-18-windows-wsl2-docker-path.md)
made Windows the one platform whose Postgres and Redis ran in containers
(`docker-compose.windows.yml`). [`2026-07-28-windows-agent-runner-only.md`](2026-07-28-windows-agent-runner-only.md)
then scoped a native Windows unit to `agent-runner`, with no local data plane,
and kept "run the gateway inside WSL2" as the workaround for a gateway on
Windows hardware. The compose file survived both as a hand-run convenience for
that WSL2 gateway.

It no longer describes a data plane the gateway can own:

- It seeds the container superuser from `AVA_DB_ADMIN_PASSWORD` and relies on
  a start-time `_docker_rekey_superuser`. Neither exists any more: the schema
  owner is NOLOGIN, the administrator is the OS user over `peer` on an
  owner-only socket, and application processes receive only a write
  generation's logins in their launch environment
  ([`2026-09-26-internal-data-plane-always-authenticated.md`](2026-09-26-internal-data-plane-always-authenticated.md)).
- The gateway's custody of its data plane is native: per-cluster instances
  under `$AVA_HOME` on the cluster's own ports, `pg_hba` written before the
  postmaster starts and proven behaviourally, PgBouncer owned beside it, and
  every administrative session (release fencing and PITR included)
  custody-checked against the home's own postmaster data directory
  (`$AVA_HOME/pg`). A container publishing a TCP superuser on fixed ports
  satisfies none of that.

## Decision

User ruling (2026-09-28): delete `docker-compose.windows.yml`. A Windows machine
runs `agent-runner` only. A gateway on Windows hardware runs inside WSL2
through the ordinary native Linux path (`ava start --serve-gateway
--serve-agent-runner` with native Postgres, Redis and PgBouncer). Live
references are removed: `gateway/windows-gateway.md` and the `windows` branch
of `scripts/provision/database.sh`, which now reports that a Windows unit has
no local data plane.

## Alternatives rejected

- **Rewrite the compose file for the new custody model.** It would need a
  NOLOGIN owner, an OS-user `peer` administrator on an owner-only socket, a
  pooler, per-cluster ports and names, and process identities the release
  fence and PITR can capture, all from inside a container runtime. That is a
  second data-plane implementation for one convenience, and the native WSL2
  path already provides the same gateway.
- **Keep the file as an unsupported example.** A file that fails at its first
  required variable, and names a function that no longer exists, reads as a
  supported path and misleads exactly the reader it was written for.

## Consequences

- Windows setup has one shape per role: native `agent-runner` on Windows, or
  the whole Linux path (gateway included) inside WSL2.
- A WSL2 gateway installs the native data plane in the distro, as any Linux
  gateway does (`scripts/provision/database.sh`).
- The two earlier decisions stay as written; this entry supersedes their
  container data plane only.
