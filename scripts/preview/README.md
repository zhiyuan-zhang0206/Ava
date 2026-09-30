# Local branch preview

`local.py` resolves a local branch, fetched remote ref, or commit to one immutable
commit before preparing a disposable cluster. It can exercise an unmerged branch
without waiting for CI or a production gateway. Its result is evidence for that
revision and profile; it does not replace CI or authorize production promotion.

```bash
python3 scripts/preview/local.py run --ref codex/my-change
python3 scripts/preview/local.py run --ref origin/main --keep
python3 scripts/preview/local.py check /absolute/path/to/run
python3 scripts/preview/local.py stop /absolute/path/to/run
```

`--repo /path/to/repo` selects another local repository (fetch remote branches
there first); `--root` selects another parent directory. Uncommitted working files
are not included, and a running preview does not follow later branch movement;
repeat `run --ref ...` for the next commit. The target revision must implement this
start entry, the core service roster and the scripted `message_flow` scenario; an
incompatible revision fails visibly. Git, uv, Node/npm and the native data-plane
prerequisites must be available. Run trusted branches only: home isolation is not
a sandbox for arbitrary source code.

The controller records the commit and requested ref under `~/.ava-previews/<run>`.
It prepares a detached worktree, the candidate's own Python environment and
frontend dependencies. It then invokes the candidate's normal `ava start` with
`--worktree`, a private configuration file, and a persisted allowlist of gateway,
frontend, ops and agent-host. Initialization, retry and stop belong to the normal
lifecycle; the controller has no alternate service launcher.

Gate is not in that allowlist, so open the printed frontend app URL directly: the
browser loads Next.js from the app port and calls the gateway cross-origin. The
controller configures no origin: the gateway's derived CORS allowlist includes the
loopback origins of the app port that start reserved for this home
(`AVA_APP_PORT`), so the allowed origin cannot drift from the allocated block.

Each run owns its home, registry, port reservation and native Postgres, Redis and
PgBouncer. It inherits only a small OS environment allowlist, with no production
URLs, bearer, provider keys, Python redirection or telemetry export. The model is
scripted, while agent creation, graph execution and `print(1 + 2)` run through the
actual gateway and agent-host. Success requires the recorded execution body to be
`3`, not merely a model response or a timestamp containing that digit. The
following observer check requires identity probes for all four services, a
frontend HTTP response and an authenticated CORS response for that exact browser
origin, so a gateway that does not allow the reserved app origin fails verification.

On macOS the normal chain is `launchd -> signed helper -> ava-root`. Each run
uses its own helper artifact directory and home-specific job, preserving the
stable signing identity without replacing another home's artifact. Signing and
permission prerequisites must already be available. Linux runs ava-root without
a helper. This controller uses POSIX process and lock APIs.

A run normally stops in `finally`; `--keep` retains only a successful preview.
Teardown calls normal stop and destroy, then independently checks for surviving
processes, listeners and a born home left undetached. A failed cleanup stays failed in
`run.json`; the observer never deletes evidence to manufacture a clean result.
Logs and data remain for inspection. Concurrent lifecycle actions on one run are
rejected by the operation lock. Recorded foreground commands are reaped on
timeout or interruption; SIGKILL or power loss cannot run cleanup, so run `stop`
on the recorded directory when resuming. After confirmed cleanup, remove the
detached worktree with `git worktree remove --force <run>/source`, then the run
directory once its evidence is no longer needed.

This profile proves source startup and real agent execution. It does not prove
fleet update, multi-machine coordination, real provider behavior,
browser/computer permissions, or production cutover. Those require their own
maintained scenarios using the same lifecycle APIs.

## Linux lifecycle proof

Inside an isolated Linux machine with systemd, run the maintained scenario:

```bash
python3 -m scripts.preview.linux_cycle --ref codex/my-change
```

The candidate needs this scenario's observer module. The guest must already have
Python 3.12, uv, Git, Node/npm, PostgreSQL 17 with pgvector, Redis 8.2 and
PgBouncer available, plus noninteractive sudo for its private systemd unit.
For OrbStack, use an isolated Ubuntu machine with host sharing and private host
network access disabled; run this command inside the guest. The scenario does
not install OS packages or alter host/VM settings.

It checks first start, bare repeat, ordinary stop/resume, and the same home's
systemd start/stop/resume. Each successful start must immediately admit and
complete real agent execution. Independent native observations check PID1 as
root's parent, absence of a permissions helper, exact application/data process
births, listeners, service PATH, completed Redis diagnostics, stored agent rows,
and unchanged identity/configuration. systemd stop must close root/application
processes while retaining the exact data-plane births. Ordinary stop closes
both application and data processes; resume retains durable state.

Durable PTYs have their own lifetime. The scenario creates one idle terminal
through the existing terminal backend and requires its recorded host, shell,
generation and control identity to survive manager stop/resume. Other surviving
PTY workloads must also prove their native owner from the persisted record and
control socket; a process name is never sufficient. Full stop/destroy must close
these resources too. Observations retain process ancestry, command and working
directory so failed-run cleanup does not erase the classification evidence.

Every phase is recorded in `cycle-proof.json` and `cycle-<label>.json`. Cleanup
always uses ordinary stop/destroy and then independently verifies native and
registry absence. A failed phase stays failed even when cleanup succeeds; the
scenario never retries a failed admission probe or force-kills a survivor to
produce a passing result. Keep the run directory as evidence, and stop only the
named test VM after inspecting cleanup. This is lifecycle evidence, not fleet
update or production approval.

The observer requires exact root argv, executable, cwd, home/host-state/virtualenv
environment and admitted PATH, alongside native birth, ancestry and listener
custody. Other environment values are represented only by a digest; this does not
independently attest arbitrary configuration values or reveal credentials.

The `validate.sh` and `spawn-samples.sh` scripts operate an explicitly selected
already-running preview home. They resolve that checkout's gateway and credentials;
they are not cluster initialization or update entrypoints.
