# Windows Setup

**Windows runs the `agent-runner` capability, natively — no WSL2, no Docker, no
a session substrate.** It enrolls against a gateway on macOS or Linux.

Windows cannot carry the `gateway` capability: that means owning a Postgres +
Redis data plane, and there is no native Windows redis path (plus three further
blockers). The measurements are in
[`future/infra/windows-gateway.md`](../gateway/windows-gateway.md);
the scoping decision is
[`2026-07-28-windows-agent-runner-only.md`](../decisions/2026-07-28-windows-agent-runner-only.md).

To run a gateway on Windows *hardware*, run it inside WSL2 — that is Linux, so
the whole Linux gateway path applies, and this guide does not cover it.
**A WSL2 distro without sudo can still install**: the provision scripts
(`scripts/install-cli-tools.sh`, `scripts/provision/database.sh`,
`scripts/provision/node.sh`) presence-check the binaries they would apt-install
(CLI tools / node ≥ 20.9 / pg17 under `/usr/lib/postgresql/17/bin` /
`redis-server` / `pgbouncer`) and skip apt — plus the root-requiring keyring
and apt-sources writes — when everything is already present. `ava start`
provisions the per-cluster Postgres itself under `$AVA_HOME/pg` from a cached
template, so no system data dir needs bootstrapping. When some packages are
missing, the scripts degrade to a warning listing what to install later via
`sudo apt-get install ...` instead of failing the install. One runtime note:
the PgBouncer pooler is ON by default and `ava start` fail-fasts when its
binary is missing — set `AVA_PGBOUNCER_ENABLED=false` in the cluster `.env` to
(converge then rewrites `AVA_DB_URL` to the direct Postgres port — one URL
either way)
run without it. **A
WSL2 unit and a native Windows unit on one physical machine must be given
different daemon health ports**, because WSL2's default networking forwards
Windows' own `localhost` into the distro one-way (Windows → WSL2, NAT): the two
units are separate OS instances sharing one loopback namespace, so Windows'
`localhost:8102` may resolve to whichever process WSL2 happened to forward
rather than the one being probed. NAT is WSL2's default, but not its only mode
— switching to *mirrored* networking (`.wslconfig`: `networkingMode=mirrored`)
shares the port space in both directions, turning a collision that was latent
under NAT into an immediate one (issue #1152).

`ava enroll` on a WSL2 host handles the common case without an operator having
to know any of this: **when `--health-port-base` is omitted, a WSL2 unit
auto-applies a fixed reserved base instead of the shared defaults a co-located
native Windows unit would also fall back to** — so a fresh install on either
side of the pair, done in either order, does not collide by default. Pass
`--health-port-base <N>` explicitly only to pick a *different* base — e.g. a
third unit sharing the same box, or two WSL2 distros on one machine (the
auto-default is one fixed value, so a second WSL2 install still needs an
explicit base). Which base is free beyond the reserved one is something only
the operator knows, so nothing scans for it; `ava start` refuses to launch onto
a port another unit already answers on regardless, which is what catches any
collision this default does not preempt (see Troubleshooting).

## Prerequisites

| Component | How to install |
|---|---|
| **Python 3.12** | `winget install python-3.12`, or `uv python install 3.12` |
| **uv** | pinned 0.10.2 release asset with sha256 verify — see [Pinned uv install](#pinned-uv-install) below |
| **Git** | `winget install Git.Git` |

The session layer is `shared/winproc.py`, no Docker (no local data
plane), no Node (the frontend is a gateway service).

## Pinned uv install

uv is installed from a pinned GitHub release asset — fixed version + sha256,
the same operator-approved version `scripts/provision/toolchain.sh` and CI
run — instead of the astral installer's rolling latest. PowerShell (x86_64):

```powershell
$ErrorActionPreference = "Stop"
if (Get-Command uv -ErrorAction SilentlyContinue) {
  Write-Host "uv already present ($(uv --version))"
} else {
  $v = "0.10.2"
  $expected = "493ebbe0e06128d6ee4905e1ed5e2a433fb0f7cfc08b0eaca9fab4ca76778ae1"  # x86_64-pc-windows-msvc
  $tmp = Join-Path $env:TEMP "uv-$v"
  New-Item -ItemType Directory -Force $tmp | Out-Null
  $zip = Join-Path $tmp "uv.zip"
  Invoke-WebRequest -Uri "https://github.com/astral-sh/uv/releases/download/$v/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
  $actual = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
  if ($actual -ne $expected) { throw "uv $v sha256 mismatch: got $actual, expected $expected" }
  Expand-Archive -Path $zip -DestinationPath $tmp
  # The 0.10.2 Windows zip carries uv.exe at the zip root (no uv-<tag>/ dir,
  # unlike the POSIX tarballs toolchain.sh unpacks).
  $dest = Join-Path $env:USERPROFILE ".local\bin"
  New-Item -ItemType Directory -Force $dest | Out-Null
  Copy-Item (Join-Path $tmp "uv.exe") $dest -Force
}
```

ARM64 hosts swap the asset and hash for `uv-aarch64-pc-windows-msvc.zip`
(`826e4ee3a03ec245e54c449e272fdf8aab749e039cc49c950ad43cc13702221f`). Both
values live in `shared/brew_pin.py` (`UV_WINDOWS_ASSET_SHA256`); the contract
test `tests/scripts/test_toolchain_uv_pin.py` asserts this guide never drifts
from them.

## Setup

You need three facts from the gateway operator: the gateway URL, the cluster
secret, and a name for this machine.

```powershell
# 1. Clone to the canonical home
mkdir $env:USERPROFILE\.ava
git clone https://github.com/zhiyuan-zhang0206/Ava.git $env:USERPROFILE\.ava\source
cd $env:USERPROFILE\.ava\source

# 2. Install deps + the `ava` CLI into .venv\Scripts\
uv sync

# 3. Join the cluster. --machine-host is THIS host's address on the cluster's
#    private network (its VPN overlay IP / private hostname) — the gateway dials
#    this runner's ops server there, so localhost only works if the gateway
#    shares this box (it cannot, on Windows).
$env:AVA_CLUSTER_SECRET = [System.Net.NetworkCredential]::new(
  '', (Read-Host -AsSecureString 'Cluster secret')
).Password
.venv\Scripts\ava enroll `
  --gateway https://<gateway-host>:8000 `
  --machine-name <this-machine> `
  --machine-host <this-host-private-ip>
Remove-Item Env:AVA_CLUSTER_SECRET

# 4. Bring up this host's services
.venv\Scripts\ava start
.venv\Scripts\ava status
```

`ava enroll` verifies the runner's projected connection facts (db/redis URLs,
event channel) without caching them; every runner process re-fetches them from
the gateway at startup. It atomically writes only its owner-readable bootstrap
identity/reachability env. It does not birth a cluster — an enrolled runner's
cluster identity *is* the gateway URL plus the secret it enrolled with.

Daemon health ports are **not** among those facts: a port block belongs to the
cluster, but the collision domain is one machine's localhost namespace, and the
two diverge exactly when a machine carries two of them. Add
`--health-port-base <N>` when another Ava unit shares this machine's loopback;
omit it otherwise and each daemon takes its shared default (8102-8109) — except
on WSL2, which auto-applies its own reserved base instead (above).

`<N>` is a **block base on the allocator's grid** — `18000 + k*16`
(`shared/port_block.py`), the same grid `install.sh` hands out to clusters, so a
hand-set unit's ports are comparable with what `ava cluster ls` prints. `18112`
puts the restarter on 18115 and the ops server on 18119. Pick a base **no local
cluster already owns**: run `ava cluster ls` on this machine and take a base
outside every block it lists. Nothing checks this for you — the arithmetic
cannot see the registry, and `ava start` only catches the overlap later, once
the other cluster's daemons are actually up. A base already present in this
unit's `.env` — from a prior explicit choice or a prior WSL2 auto-default —
survives a bare re-enroll that omits the flag; only an explicit
`--health-port-base` changes it.

## What differs from POSIX

| Concern | POSIX | Windows |
|---|---|---|
| Session supervision | session backend — native processes for services/agents, per-session pty hosts for agent shells | `shared/winproc.py` — a detached process (cmd.exe only when the command's own syntax needs it, and then with a console so it does not swallow the output — see the module docstring), JSON session records under `$AVA_HOME\run\sessions\`, pid+create_time liveness, Ctrl-Break then tree kill |
| Session isolation | native sessions double-fork to init; agent shells are pty sessions, each in its own detached host | nothing reparents — a session stays its spawner's child, so the kill walk stops at other sessions and at the caller's own ancestry (`winproc._spared_pids`, [decision](../decisions/2026-07-29-windows-session-kill-boundaries.md)) |
| Subprocess timeouts | killing the direct child usually collapses the pipeline with it | `C:\Program Files\Git\cmd\git.exe` is a launcher stub for the real git, so a timeout that kills only the direct child leaves a live `git` → `sh` → `ssh` tail (measured: 66 + 63 + 66 orphans). Every bounded call goes through `shared/proc.py:run_bounded`, which kills the tree; git also gets `shared/gitenv.py:git_env()` so `GIT_SSH_COMMAND` outranks a stray `core.sshCommand` in the global gitconfig |
| OS-scheduled jobs | launchd / cron | `schtasks`, under task folder `\Ava\<home-slug>\` (`shared/os_schtasks.py`). Registered from a task definition (`/Create /XML`, kept at `$AVA_HOME\run\schtasks\<kind>.xml`) so the power and time-limit settings are explicit — see [Power](#a-runner-needs-ac) below |
| Boot autostart | launchd / crontab `@reboot` | a logon task, registered automatically by the converge phase on every `ava start` — nothing manual. It runs `ava boot` (retry loop) rather than `ava start`: `schtasks /RI` does not apply to a logon trigger, so the retry cannot live in the scheduler. **Logon, not machine start** — see [Reboot](#a-reboot-with-no-logon-does-not-bring-ava-back) below |
| venv layout | `.venv/bin/` | `.venv\Scripts\`, `python.exe` / `pythonw.exe` |
| `ava` on PATH | symlinked to `~/.local/bin` | not symlinked — invoke `.venv\Scripts\ava` |
| Headed browser | `ava-browser` session **is** Chrome (`os.execvp`) | `ava-browser` session is the launcher, which stays Chrome's parent and waits on it — Windows has no exec, so an `execvp` would spawn-and-exit and leave the supervisor tracking a dead pid while Chrome ran on |
| `browser-mcp` + the `chrome` MCP | run | **gated off** — the wrapper→daemon transport is an AF_UNIX socket, which Windows does not have. Chrome itself runs and is reachable over CDP; agents get no `ava.mcps.chrome`. [Port →](../services/browser/windows-browser-mcp.md) |

`ava.shell` sessions and `capture_pane` are PTY features the Windows session
backend does not implement; an agent's persistent shells are unavailable there.

## Operational limits of the scheduled jobs

Two limits are structural, not bugs to be fixed later. Both are consequences of
how Windows schedules work, and both are load-bearing for how much you can trust
a Windows runner to recover on its own.

### A runner needs AC

**Treat an Ava Windows runner as a machine that stays plugged in.** Task
Scheduler's own defaults are written for user convenience jobs, not supervisors:
`DisallowStartIfOnBatteries` and `StopIfGoingOnBatteries` both default to true, so
on a laptop every Ava job is *stopped* the moment the machine is unplugged and
refuses to start again until it is plugged back in — including the minute-cadence
watchdog probe, which is the OS-level supervisor of last resort and the only thing
that revives a dead watchdog. With `StartWhenAvailable` also defaulting to false,
the ticks missed in between are never caught up.

`shared/os_schtasks.py` sets all three the other way (batteries do not block or
stop a job; a missed tick runs on resume), so an unplugged runner keeps
supervising itself. What it cannot do is keep working while the machine sleeps or
hibernates — `WakeToRun` is deliberately false, because a job that woke a sleeping
laptop every 60 seconds would be hostile. So the practical rule stands: on
battery, Ava keeps running but the box is far likelier to suspend, and a suspended
runner is a runner that is doing nothing until someone opens the lid.

Each job also carries an explicit `ExecutionTimeLimit`, because a Windows task's
instance policy is `IgnoreNew` (a new invocation is dropped while the previous one
is still running) and the default limit is 72 hours — one hung probe would block
every later probe for three days. The bound is what makes that recoverable, and the
values are per job kind: 5 minutes for a watchdog probe, 30 minutes for a health
probe (it can launch an auto-rollback), and **no limit for the boot job**, whose
retry loop is uncapped by design on all three platforms.

Check what a task is actually carrying with:

```powershell
schtasks /Query /XML /TN \Ava\<home-slug>\watchdog-probe-agent-runner
```

That output is directly comparable to the definition Ava wrote at
`%USERPROFILE%\.ava\run\schtasks\watchdog-probe-agent-runner.xml`. Do not fix drift
by hand in the Task Scheduler UI: converge re-registers every task with `/F` on
every `ava start` and `ava cluster update`, so a manual edit lives until the next one.

### A reboot with no logon does not bring Ava back

The autostart job uses a **logon** trigger, so it fires when the user logs on, not
when the machine powers up. That is deliberate: the alternative (`ONSTART`) fires
earlier but runs as SYSTEM, which is the wrong identity — `$AVA_HOME` and the
supervised `winproc` sessions belong to the interactive user, and a cluster brought
up by SYSTEM would not own the sessions it is supposed to supervise.

The consequence: **after an unattended reboot with no interactive logon — a
Windows Update restart overnight, a power cut, a remote reboot nobody signs back
into — this host stays down until somebody logs in.** Nothing else covers it: the
watchdog probe revives a dead watchdog, not a cluster that never started, and its
own task is also user-scoped. The gateway will report the machine offline.

Mitigations, in the order they cost:

- Enable Windows' own **automatic sign-in after an update restart** (Settings →
  Accounts → Sign-in options), which turns the common case into a logon.
- Keep the box configured so reboots are attended (deferred update restarts).

*Future work, not built:* a second, SYSTEM-level task on an `ONSTART` trigger whose
only job is to wait for an interactive session to appear and then hand off, leaving
the actual bring-up in the user's identity. It has to be a separate task rather
than a change to this one, because the whole point is that the bring-up must not
run as SYSTEM.

## Troubleshooting

For a **WSL gateway** on this Windows host, use the separate
[unattended WSL boot procedure](wsl-gateway-boot.md). It preserves the native
Windows runner's interactive task identity and does not automatically register
or alter any existing task.

**`ava start` prints "the per-cluster data plane is not supported on Windows"** —
this host was asked to serve the gateway capability. Check `%USERPROFILE%\.ava\.env`
for `AVA_MACHINE_SERVE_GATEWAY`; it should be absent or `false`. An enrolled
runner never sets it.

**The gateway reports this machine offline** — the gateway dials the address
given as `--machine-host`. Confirm it is reachable *from the gateway*, not just
locally, and that the ops server is listening (`ava status`). Re-run `ava enroll`
to change it.

**`identity mismatch on http://localhost:8102/healthz`**, or `ava start`
refusing with *"another unit already answers on this unit's daemon health
ports"* — another Ava unit shares this machine's loopback namespace, and
Windows' loopback forwarding is routing the probe to its daemon instead of this
one's. This should not happen against a WSL2 unit enrolled after this fix (it
auto-defaults off the shared ports; see the caveat above) — if it still does,
either that WSL2 unit was enrolled before the fix and needs a re-enroll to pick
up the default, or a *third* unit is sharing the box (a second WSL2 distro, a
container) and needs its own explicit base. The `home=` in the message names
which unit is answering. Give one of them its own block: re-run `ava enroll`
with `--health-port-base <N>` there, then restart it.

**Agents start and immediately terminate** — usually a missing model API key in
`%USERPROFILE%\.ava\.env`. `ava enroll` persists identity/reachability and verifies
the cluster projection; runner processes re-fetch connection facts at startup, and
model credentials remain local.

**Scheduled jobs not firing** — `schtasks /query /tn \Ava\<home-slug>\autostart`.
The home slug comes from the `$AVA_HOME` path; a second checkout gets its own
folder, so jobs never collide across clusters. If the task exists and its last run
result looks like it never started, check the two structural limits above before
anything else: the box may have been on battery under the old settings, or it may
have rebooted with nobody logging in.

**`ava start` fails at the OS-jobs converge step with a schtasks error** — the task
definition was rejected. The definition Ava tried to register is on disk at
`%USERPROFILE%\.ava\run\schtasks\<kind>.xml`; the `schtasks` stderr is in the
converge output. Converge fails fast here on purpose: a cluster that came up
believing it was supervised when the scheduler refused the job is the worse
outcome.
