# "What version is running" is process state, not a disk bookmark

## Context

Three signals claimed to answer "what commit is this node running", and all
three are files or reads of the checkout:

| signal | what it actually is |
|---|---|
| `head_sha` | `git rev-parse HEAD` in the checkout, right now |
| `installed_sha` | file — what `uv sync` + migrate last completed against |
| `running_sha` | file — written by `ava start` right before it launches services |

`running_sha` was the one the roster displayed as the live process's commit,
and `cli/commands/cluster.py:_code_cell` was built to flag `running_sha !=
head_sha` as "the checkout advanced but the process was not restarted".

That comparison cannot produce its own signal. `_record_running_sha` runs
unconditionally at `cli/commands/start.py:338`; the very next line calls a
launcher whose per-session loop prints `✓ <session> already running` and
`continue`s. A start that restarts nothing still advances the bookmark to the
new HEAD, so exactly in the state the cell exists to catch, bookmark and
checkout agree and the cell reads clean.

The field comment on `ClusterStatus.running_sha` had described the intended
semantics accurately — "the commit THIS PROCESS is actually running" — for as
long as the field existed. It was documentation of an intent the implementation
never had.

Found while diagnosing a Windows unit that reported `serve_gateway: true` for
two days after the file that value derives from was changed to `false`. The
daemon had cached its capability set at boot; `ava start` skipped it because it
was running, and rewrote the bookmark anyway. `head_sha`, `running_sha`, and
the start command's own `[✓ aligned]` all agreed the node was current.

## Decision

Each long-running process freezes its own commit at boot and answers with that.

`shared/process_sha.py` holds one module-level capture. `freeze()` resolves the
commit of the tree the module was loaded from (via `__file__`, not the cwd) and
is called from `init_gateway_process` — the top of every gateway-style
process's `main()`. `get()` returns only what `freeze()` stored and has no git
call of its own; a process that never froze reports unknown.

Status surfaces read `process_sha.get()`. The `running_sha` file keeps its
remaining and legitimate job: the baseline `ava update` diffs against, where
"what did we last deliberately start from" is the right question.

Each daemon also publishes its capture on its own `/healthz`, alongside the
`name`/`pid`/`home` identity triple already there, and `probe_daemon` carries it
into the probe detail.

## Alternatives rejected

**Read git lazily on first `get()`.** Removes the boot-time plumbing. It also
recreates the bug: the first read in a long-lived daemon can land days after a
rollout, and it answers with the checkout as it is *then*. An implementation
that can reach git at report time is an implementation that reports the
checkout, whatever the variable is named.

**Stamp the commit into the child's environment at spawn.** Frozen at exec by
construction, and visible from outside via `ps eww`. Rejected because it is the
*spawner's* commit, not the child's: when a watchdog still on old code respawns
a daemon, the daemon loads new code from disk and would be labelled with the
watchdog's old commit. It also needs every spawn path to cooperate, and gets
nothing for a process started outside them.

**Have the status probe read each daemon's memory from outside** (`/proc/<pid>/environ`,
`ps eww`, WMI). Would give per-daemon coverage from one place, at the cost of
three platform-specific implementations — and Windows is a platform we are
committing to, not dropping.

**Make a commit mismatch fail `probe_daemon`.** Tempting: the watchdog would
then restart stale daemons by itself. Rejected for now because a daemon on old
code is *alive*, and failing it would make every watchdog respawn its daemon the
instant a rollout advances the checkout — racing the orchestrated restart that
is already handling it. Reporting and acting are separated deliberately; acting
is a policy change that deserves its own decision.

## Consequences

- The roster's `code` column speaks for **the process that answered the probe**
  — the ops daemon on an agent-runner, the gateway on a pure gateway. A sibling
  daemon respawned at a different commit is not covered by that one cell; its
  commit is on its own `/healthz`. Aggregating the per-daemon view into a single
  machine-level verdict is left open.
- Processes that come up outside the supervised path, and dev gateways under
  `AVA_GATEWAY_RELOAD=1` (uvicorn re-imports in a spawned worker), report
  unknown and render `—`. For hot-reload that is the honest answer: the running
  code is not a commit.
- One `git rev-parse` per gateway-style process boot, bounded and non-fatal —
  a source tree that is not a git checkout simply has no commit to report.
- Existing `_code_cell` and frontend `codeDrift` logic is untouched. The
  display was always right about what it wanted to show; only its input changed.
