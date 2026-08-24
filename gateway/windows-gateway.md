# Windows gateway support

Windows carries `agent-runner` only. This is what a gateway would additionally
require, measured against the tree on 2026-07-28 rather than estimated. The
decision to scope Windows this way is
[`2026-07-28-windows-agent-runner-only.md`](../decisions/2026-07-28-windows-agent-runner-only.md).

Available today for a gateway on Windows hardware: run it inside WSL2. That is
Linux, so the whole Linux path applies unchanged — native pg/redis via
`install.sh --role gateway`, or containers via `docker-compose.windows.yml`.

## The four blockers

### 1. Redis has no Windows story at all

Not a rough port — an absent one.

- `shared/runtime_binaries.py` vendors Postgres for `darwin` and
  `linux-x86_64`; `_platform_key()` raises for Windows. Redis is not vendored on
  any platform yet ("a prebuilt we publish — not here yet").
- `_redis_server_bin()` / `_redis_cli_bin()` (`cli/commands/_cluster_instance.py`)
  resolve the `redis@8.2` brew keg on macOS and a bare `redis-server` elsewhere. No `.exe`, no
  Memurai branch.
- `_start_redis` passes `--daemonize yes`. Windows redis forks do not implement
  it.

Three possible directions, none of them started: vendor a Windows redis build,
shell out to Memurai (closed-source, licensing unexamined), or containerize the
redis leg alone.

### 2. Self-update — closed for the `ops/cluster*` family, open for schedules

**Closed.** `spawn_update`, `spawn_rollout`, `spawn_restart` and
`unpause_local_cluster` used to each build a literal raw session spawn around a
POSIX `sh` one-liner (`{ … }`, `2>&1 | tee -a`, `$?`), while the probe and the
kill paths already dispatched through `get_backend()`. Liveness and teardown
were Windows-capable and every actual orchestration failed — a partial port
that is worse than none, because the unit comes up, reports healthy, and
silently cannot take an update. The three orchestration spawns
(`spawn_update` / `spawn_rollout` / `spawn_restart`) share
`ops/cluster_session.py:_spawn_detached_session`, which dispatches on
`PlatformBackend.is_posix()` and hands a `cmd /c` chain to winproc on Windows;
`unpause_local_cluster` respawns the restarter (the one service among them)
through the session backend directly (`get_backend().new_session`) —
post-2026-08 it lands on the native POSIX supervisor, on Windows it is
winproc.

Spawning the updater was only half of it: the session it spawns is the ops
daemon's child (Windows reparents nothing), so the updater's own `ava restart`
killed itself the moment its stop reached `ava-ops`. Closed on the kill side —
`winproc.kill_session` prunes other sessions and the caller's ancestry out of the
tree walk
([decision](../decisions/2026-07-29-windows-session-kill-boundaries.md)).

**Still open.** `gateway/schedule_manager.py:_launch` had the original shape
(a raw spawn) while its `_live_ids`/`_kill` siblings were already
backend-dispatched — the same half-port, one layer over. Schedules are a gateway
service, so this one is genuinely blocking for a Windows gateway and irrelevant
to a Windows agent-runner.

### 3. `milvus` cannot install, and nothing gates it

`milvus-lite`'s dependencies (`faiss-cpu`, `grpcio`, `numpy`, `pyarrow`) all
carry `marker = "sys_platform != 'win32'"` in `uv.lock`, so a Windows venv gets
the package without its runtime. `services/milvus/daemon.py` then calls
`os.execvp`, which on Windows spawns-and-exits rather than replacing the image,
so the pid `winproc.new_session` recorded is not the surviving process.

`ops/spec.py:_gate_reason` has no milvus branch, so the service is
unconditionally in the Windows start roster and fails every time.

### 4. PgBouncer is POSIX-only and on by default

`cli/commands/_pgbouncer.py` signals with `os.kill(pid, SIGHUP)` /
`os.kill(pid, 0)`. On Windows `os.kill(pid, 0)` **terminates** the target — the
hazard `shared/proc.py` documents and routes around. PgBouncer is enabled by
default (`shared/config/data_plane.py`) and is part of `ensure_cluster_instance`.

## Also missing

- **Birth.** `scripts/install.sh` is bash and dies on `uname` before role
  dispatch; `cli/install_cluster.py` has no platform branch at all. A Windows
  gateway needs a birth path that is not a shell script.
- **Unix sockets throughout the data plane.** `_pg_socket_dir()` hardcodes
  `/tmp/ava-pg-<slug>`, `pg_admin_url()` is
  `postgresql://<user>@/postgres?host=<socket-dir>`, `_start_pg` passes
  `-c unix_socket_directories=…`, and `_pg_hba_body()` writes a `local` line.
  Windows Postgres supports none of it.
- **`_pg_bin()`** has no Windows branch and yields the Linux path;
  `PG_BIN_WINDOWS` exists in `shared/pg_tools.py` but is only reachable through
  `platform_backend.pg_binary_path`, which `_cluster_instance` never calls.
- **`memory-indexer`** registers a `SIGTERM` handler for graceful shutdown that
  never fires on Windows, and cold-starts against milvus.
- **Teardown symmetry.** `stop_cluster_instance()` calls `pg_ctl` / `redis-cli`
  with no `supports_data_plane()` guard, so it would fail even on a host that
  never brought a data plane up.

## Harvested from the abandoned attempts

Two independent WIP branches attempted this and are fully mined; nothing below
needs to be re-read from them.

`win-native-support-wip-20260726` contributed the runtime platform fixes
(`WindowsSelectorEventLoopPolicy`, the MCP-daemon subprocess split, the
`os.execv` argv0 resolution, `Scripts\python.exe`) — **all of these are already
on main**, arrived by other routes. Its data-plane attempt hardcoded container
names and ignored the registry-allocated ports, so two co-located clusters would
adopt each other's containers.

`ava-windows-scheduling` contributed the `schtasks` work, which is on main in a
**better** form: main's `os_schtasks.task_name`/`delete_task` take an explicit
slug, where the branch fell back to the calling process's own slug — the bug
that made a `cluster destroy` deregister prod's jobs.

The one idea worth keeping is its **per-cluster-parameterized container data
plane**: container names, published ports, volume names and the data-plane
identity all driven from the cluster's registry record via compose `${VAR}`
substitution, so co-located clusters do not collide. If the container direction
is ever taken, that shape is right and these three defects in that
implementation are not:

1. **The compose file seeds `POSTGRES_USER` from the cluster identity**, making
   the container's only superuser the same role `provision_database` then runs
   `ALTER ROLE … LOGIN NOSUPERUSER` against. Irreversible: no role is left that
   can grant superuser back.
2. **The redis ACL user is never created.** `_ensure_redis_acl` is reachable
   only through `_start_redis`, and the container branch returns before it —
   while the generated `AVA_REDIS_URL` authenticates as that user.
3. **`stop_docker_data_plane()` passes no `env=`**, so compose resolves the
   container names to their defaults and stops the wrong containers (or fails
   the required-variable check), and a non-zero return is swallowed into
   success.

A container data plane also needs a compose project name (`-p`) per cluster;
without one the project defaults to the compose file's directory and a `stop`
reaches every co-located cluster's containers.
