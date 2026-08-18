# Windows carries the agent-runner capability only

## Context

[`2026-06-18-windows-wsl2-docker-path.md`](2026-06-18-windows-wsl2-docker-path.md)
decided that Windows support means WSL2: gateway *and* agent-runner both run
inside the distro, reusing the Linux code unchanged, with pg/redis in
containers. It named a native port as "open but unscheduled" and said that if
taken, it supersedes that entry rather than rewriting it.

Half of it was taken. [`2026-07-19-agents-become-detached-native-processes.md`](2026-07-19-agents-become-detached-native-processes.md)
introduced `shared/winproc.py`, and a native Windows agent-runner grew around
it: a native session supervisor, `schtasks` for the three OS-scheduled
jobs, `Scripts\python.exe` resolution, psutil-based liveness. It runs in
production today. The gateway half was never taken, and nothing recorded that
the two halves had diverged — so the repo accumulated four incompatible answers
to "what does Windows support mean": this decision (WSL2 for both), the setup
guide (WSL2 for both), `AGENTS.md` (native pg/redis, no Windows carve-out at
all), and `future/` (Windows excluded).

Trying to close the gap by making Windows gateway-capable ran into what the
native port actually costs on that half:

- **Redis does not exist.** Not a port with rough edges — there is no native
  Windows redis path anywhere in the tree. It is absent from the vendored
  binaries (`shared/runtime_binaries.py`), there is no Memurai or equivalent,
  and `_start_redis` passes `--daemonize yes`, which Windows redis forks do not
  implement.
- **Self-update is structurally impossible.** `spawn_update`, `spawn_rollout`,
  `spawn_restart` and `unpause_local_cluster` build literal
  a raw `["session-binary", "new-session", …, $SHELL, "-lc", <POSIX one-liner>]` argv
  (`ops/cluster.py`). They do not route through `session_backend` the way the
  rest of the session layer does. A gateway that cannot self-update cannot take
  part in a rollout, which is most of what a gateway is for.
- **`milvus` cannot install and is not gated.** `milvus-lite`'s dependencies all
  carry `marker = "sys_platform != 'win32'"`, and `ops/spec.py:_gate_reason` has
  no milvus branch — so the service is always in the Windows start roster and
  always fails.
- **PgBouncer is on by default and POSIX-only**, and its liveness check is
  `os.kill(pid, 0)`, which on Windows *terminates* the target.

Postgres is the only leg with a plausible short path, and one leg is not a data
plane.

## Decision

A Windows unit carries `agent-runner` and nothing else. It is enrolled against
a gateway running on macOS or Linux (`ava enroll`), which is platform-neutral
and needs no data plane locally.

Windows gateway support moves to
[`future/infra/windows-gateway.md`](../future/infra/windows-gateway.md)
with the gap list above, so a future attempt starts from measurements rather
than rediscovering them.

Every Windows claim in the repo is reconciled to this one sentence — README,
QUICKSTART, the setup guide, `AGENTS.md`, and the `future/` entries that
described the pre-`winproc` world.

The refusal is made to say so. `ensure_cluster_instance` already failed on
Windows; it said "not yet supported on this platform. Follow-up: bundle or
Docker-host a per-cluster instance", which reads as an unfinished TODO rather
than a topology. It now names what to do instead.

## Alternatives rejected

**Run the gateway inside WSL2** (what the 2026-06-18 entry decided). Still
works, and is still the answer for someone who wants a gateway on Windows
hardware — it is Linux, and the Linux path is unchanged. Rejected as *the
documented Windows story* because it makes "Windows support" mean two different
architectures at once: the agent-runner native on the host, the gateway inside a
VM on the same host. A reader cannot hold both, and the setup guide written that
way is the guide nobody could follow. It is recorded in the future doc as the
available workaround, not as the platform's shape.

**Containerize the data plane on native Windows.** What both abandoned WIP
branches were reaching for, and the shape the 2026-06-18 entry endorsed
("Windows is the one platform where the data plane is containerized"). Rejected
now because it fixes only the first of the four blockers: the self-update layer
is still raw session argv, milvus still cannot install, PgBouncer still cannot be
signalled. Shipping a gateway that comes up and then cannot be rolled out is
worse than one that refuses to come up, because the first failure is silent.

**Leave the docs alone and just not support it.** The four contradicting answers
predate this decision, and the front door (`README.md`, `QUICKSTART.md`) is the
part an open-source reader hits first. A reader who follows the current
`windows-setup.md` runs `install.sh --skip-native-infra`, a flag that does not
exist.

## Consequences

- Windows cannot be a single-box deployment. A Windows user needs a POSIX
  gateway somewhere — another machine, or WSL2 on the same box.
- The native Windows work already merged (winproc, schtasks, the platform
  backend) keeps its full value; it was always agent-runner work.
- `docker-compose.windows.yml` stays, but as what it is: containerized pg/redis
  for a Linux or WSL2 gateway when containers are preferred over native. No
  Python code references it, and none should — the supported data plane is
  native per-cluster pg/redis.
- The two WIP branches (`win-native-support-wip-20260726`,
  `ava-windows-scheduling`) are fully harvested: their runtime fixes are already
  on main, their scheduling work was superseded by main's slug-parameterized
  version, and the one surviving idea — a per-cluster-parameterized container
  data plane — is recorded in the future doc along with the three defects found
  in that implementation. The branches can be deleted.

<!-- The decision stands. One observation above was narrower than it read: the
`os.kill(pid, 0)` liveness check is in `_terminate_verified`, which is shared with
`_reap_orphan_listeners` and the gate teardown and therefore runs inside `_do_stop`
on EVERY platform — not only on the POSIX-only pooler path this cited it for. On
2026-08-12 that raised `[WinError 87]` out of the middle of win's `ava restart` and
killed its self-update. Routed through `shared.proc` in the same PR as
`cli/commands/_installed_sha.py`. -->
