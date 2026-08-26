---
name: ava-self-development
description: Guides Ava kernel changes from worktree through rollout and recovery. Use before touching Ava core source, previewing a kernel change, cutting an SDK changelog, or running `ava cluster update`; skill and plugin edits use lower layers.
---

# Ava Self-Development (L4 — changing the kernel)

This is the **L4** manual of the four-layer modification model
(`decisions/2026-08-19-four-layer-modification-model.md`): changing the kernel
repo itself. Installing extensions (L1), editing skills (L2), and developing
plugins (L3) are cheaper layers with their own apply mechanisms — the
`ava-modification-layers` skill routes between them; `develop-a-plugin` covers
L3. Reach for this manual only when the change belongs in the kernel repo.

## §0 How a change to Ava takes effect (read this first)

> ⚠️ **Don't develop in the production checkout.** `~/.ava/source` is the tree the
> live cluster boots from — it is not your workspace. A stray edit, `git checkout`,
> or `git branch` here breaks every new agent spawn fleet-wide (agents start from
> on-disk code, not memory). To change Ava's code, do exactly what any other
> contributor does: clone the repo somewhere else and work in that clone — never
> treat `~/.ava/source` as your working copy. (Inside your own clone, follow the
> repo's worktree + PR convention; that convention is about your clone, not a
> license to touch prod.)

The live process's code was loaded from the **production checkout** (`~/.ava/source`) — that is where its code lives, not where you work. A change to Ava's own code does **not** take effect by editing source in your running process — or in a dev clone — and re-importing or reloading. The running process keeps the modules it already imported; production runs from its own checkout, not your working copy. There is **no in-process shortcut**.

The only way your change to Ava reaches the running cluster:

1. **Make the change in a development workspace** — a separate clone / worktree, never the production checkout. The production checkout (`~/.ava/source`) must stay on `main`: it is the tree the live process runs from, so a feature branch there runs un-reviewed code on the cluster, and the next `ava cluster update` force-checks-out the target and discards those commits. Create the worktree *from* the prod checkout (`git worktree add`); never `git checkout -b` inside it. (`ava status` warns when the prod source has drifted off `main`.) See the `ava-code` / `ava_fleet` skills for worktree + PR mechanics.
2. **Open a PR and let CI run.** CI is the single point of trust.
3. **A human reviews and merges to `main`.** (Per the collaboration rule, the user decides the merge.)
4. **Run `ava cluster update`** — the CLI (the only update entry point; `ava.self.update()` was removed 2026-08). It rolls merged `main` across the whole cluster — every agent is drained (signalled to restart, waited out per mode, stragglers force-reaped) and respawned onto the new code. Only now is your change live. Deployment is coordinated by a designated release agent (if your cluster has one) with user approval; you do not trigger it yourself.

**Anti-pattern (do not do this):** editing Ava source and then `importlib.reload(...)` (or re-`import`) to "verify" the change in the current process. It does not take effect, and reloading the live SDK mid-turn corrupts in-flight state and can crash the process. If you want to see your change run, finish it as a PR and ship it via step 4 — or let a freshly spawned / updated agent pick it up. "Edit my own running source and reload it" is never the move.

The rest of this skill is **step 4** in detail.

## §1 `ava cluster update` — mental model

Ava self-rolling upgrade in one command: **the operator runs `ava cluster update`**, and the framework handles the rest. Detailed architecture in `decisions/2026-05-09-self-rolling-release.md`; this SKILL is the agent-facing operations manual.

### Protocol

```bash
ava cluster update                  # smooth (default)
ava cluster update --mode force     # force: ~10s drain, then force-kill stragglers
```

The CLI does:

1. Serialize concurrent rollouts — one update runs at a time (cluster update lock)
2. Drain every live agent: a `restart` signal per agent makes it exit at its
   turn boundary, waited out per mode — smooth waits the configured
   `AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS` window (default 10s); force waits ~10s.
   Anything still alive after the window is force-reaped and respawned on new
   code (an agent mid-`execute_code` is cut short — the short window is
   deliberate, 2026-08-26 ruling: fast cluster unblock over graceful long execs).
   Your process goes down once and stays down while the update runs; do NOT
   expect to observe the rollout from the inside (unless you are the operator
   agent — 1818's deployment workflow runs the CLI and watches the rollout log).
3. Pull the merged `main`
4. Apply any pending database migrations
5. Bring every agent back on the new code; each fresh process leaves a
   lifecycle marker in its history:
   `[system ts] You have been updated and restarted`

(A frontend/docs-only update never quiesces — no agent restarts, the UI just
rebuilds. A backend update drains every agent; the rollout log names each
phase, and `ava cluster status` shows convergence per host.)

### Key invariants

- **Merged into main = tested + safe**: CI is the single point of trust; a passing PR means destructive migrations are allowed too. No runtime forward-compat / canary / monitoring.
- **Who can run it**: the CLI operator — deployment is coordinated by a designated release
  agent (if your cluster has one) with user approval (per the deploy-approval rule). `ava.self.update()`
  is removed, so an agent cannot trigger a rollout from inside its own process;
  concurrent rollouts are serialized by the cluster update lock.
- **Code ↔ schema must not break**: at startup every service checks that the applied database schema is new enough for the code; if it isn't, the service refuses to start and tells you to run the update flow.
- **History is preserved**: your process exits, but your saved conversation state is untouched. The fresh process comes up with the same agent id and continues reading messages.


## §1.5 Preview Cluster (validate before production rollout)

Before rolling new code to production via `ava cluster update`, validate it
in an **isolated preview cluster** — a temporary, fully-isolated Ava cluster
that runs the code you are about to ship.

### Protocol

The preview cluster is just another Ava cluster — no special CLI verbs needed.
The protocol is three steps:

1. **Create** — birth an isolated cluster from your dev worktree
2. **Validate** — run the behavioral validation suite against it
3. **Promote or destroy** — if validation passes, the code is ready for
   `ava cluster update`; tear down the preview cluster either way

### Step 1: Create the preview cluster

A cluster has no name — **its identity IS its home directory**. Birth happens
once, at install time; `ava start` afterwards is a pure bring-up that takes no
identity flags. Which cluster an `ava` acts on is fixed by which checkout it
belongs to, never by the current directory. From your dev worktree:

```bash
# Births this worktree's cluster: registry slot + port block + its own pg/redis
# + provisioned db + .env. Home defaults to ~/.ava-<worktree-dir> (--path
# overrides). Idempotent.
scripts/install.sh --worktree && uv sync && .venv/bin/ava start
```

It owns its own Postgres and Redis under that home, on its own ports — no shared
data plane with production. Use the worktree's `.venv/bin/ava`: a bare `ava` on
PATH is the production checkout's and always acts on `~/.ava`.

See `scripts/preview/README.md` for the validation environment's own discipline.

### Step 2: Validate

The validation suite lives in `scripts/preview/validate-tasks/suite.md` — a
markdown task file that exercises every core SDK surface. An agent reads it
and executes each test, producing a pass/fail report.

```bash
bash scripts/preview/validate.sh   # returns once delivered; the agent notifies when done
```

The suite covers: spawn, terminate, resurrect, restart, fork, send_message,
get_neighbors, files, shell, web.search, and notices. Each test is independent;
failures do not block subsequent tests. The agent writes the report into the
cluster's own home (path printed by `validate.sh`), outside the checkout, so a
run cannot dirty the git tree.

After validation, read it:

```bash
cat ~/.ava-<worktree-dir>/preview-validation-report.md
```

### Step 3: Promote or destroy

**If validation passes** (all tests green): the code on this branch is ready.
Merge the PR to main, then `ava cluster update` (run by the release agent with
user approval) rolls it to production.

**If validation fails**: fix the issues on the branch, re-run validation.

**Either way, tear down the preview cluster afterwards:**

```bash
# Stop it, keep its slot and data:  ava cluster down --path <home>
# Free the slot (--drop-db also removes its pg/redis data dirs):
.venv/bin/ava cluster destroy --path ~/.ava-<worktree-dir> --drop-db
```

Both take `--path`, never a name. `destroy` refuses `~/.ava` outright, so a typo
cannot point it at production.

### One-shot convenience

For CI/CD or pre-release gating, chain the three steps:

```bash
scripts/install.sh --worktree && uv sync && .venv/bin/ava start \
  && bash scripts/preview/validate.sh
# validate.sh returns before the agent finishes — wait for the report, then gate on it:
grep -q "FAIL" ~/.ava-<worktree-dir>/preview-validation-report.md && echo "Validation FAILED"
.venv/bin/ava cluster destroy --path ~/.ava-<worktree-dir> --drop-db
```

The report is the gate: it exists only once the suite finishes, and a `FAIL`
line in it blocks the promotion.

### Sample agents

To populate the preview cluster with visible activity (for manual UI review):

```bash
bash scripts/preview/spawn-samples.sh
```

This spawns coding, chat, and notice agents with mock tasks — useful for
eyeballing the FleetView before approving a release.
## §2 Failure modes + human intervention recovery

A rollout that fails does **not** auto-rollback. A human/Claude looks at the rollout log and takes over.

**Errors during the rollout (these happen in a detached background process, not in the triggering CLI; troubleshoot by reading the rollout log — see §5)**:

| Failure | Trigger | Recovery |
|---|---|---|
| `GitPullFailed` | a `git` step (`rev-parse` / `pull` / `rev-list`) exits non-zero | easily triggered if called from a dirty dev workspace; call from the production checkout, or clean the working tree first |
| `MigrationFailed` | a SQL migration crashed; its transaction rolled back, schema unchanged | look at `__cause__` for the underlying database error and locate the migration file; fix, then re-run `ava start` (it applies pending migrations) to retry |
| new code crashes on boot | the process is respawned repeatedly | on the production checkout, `git reset --hard <old_sha>`; then `ava start` to re-apply migrations on the rolled-back code (migrations only move forward — no auto-rollback) |

**`ava.self.update()` is removed** — the SDK path no longer exists; `ava.self.update()` raises `RuntimeError` pointing at the CLI. The CLI's failure surface: a refused deploy (deploy window / lock held) prints the reason and exits non-zero; `NothingToUpdate` (already on the latest code) exits 0; a rollout that left hosts mid-transition reports `INCOMPLETE` with the settle hold details. Read the rollout log (`~/.ava/logs/rollout-*.log`) and `ava cluster status` for convergence.

## §3 Usage

```bash
# Release agent (a designated agent), after the user approves the deploy:
cd ~/.ava/source && ava cluster update            # smooth (default)
cd ~/.ava/source && ava cluster update --mode force  # force: ~10s drain, then kill
```

No "broadcast / coordinate / notify peers" is needed beforehand — the rollout
signals every live agent itself (a `restart` inbound per agent, drained per
mode, stragglers force-reaped). The operator agent watches the rollout log and
`ava cluster status` until every host reports converged; each agent wakes on
the new code with the `[system ts] You have been updated and restarted`
lifecycle marker — a normal audit trail, not a fault.

## §4 Intentionally not done

| Not done | Reason |
|---|---|
| Canary release / holdout | Single-user system; runtime version splitting has lower value than complexity |
| Monitoring period | Same as above; trust shifted to the CI stage |
| Auto-rollback | Failures are handled by humans + Claude |
| Health checks | Same as above |
| Forward-compat schema | Merged into main = tested; destructive migrations (DROP/RENAME) are allowed |

Overall philosophy: **fail fast + raise + human + Claude reads the stack trace and takes over**.

## §5 Reading the logs (self-debug)

When you need to inspect what a process actually did — a failed self-update, a
crash loop, a silent stall — read its log file directly. Each agent writes a
JSONL log at `$AVA_HOME/logs/agent-<id>.log` (default `~/.ava/logs/`), one
record per line. The files are plain local files: you read your own and any
peer's the same way (a peer's file only exists on the box where that agent
runs).

```python
import json, os
from pathlib import Path

def read_log(agent_id=None, level="DEBUG", contains=None, n=100):
    """Tail an agent's log. Defaults to yourself, severity >= level,
    optional substring filter, last n records as formatted lines."""
    agent_id = ava.self.AGENT_ID if agent_id is None else agent_id
    home = Path(os.environ.get("AVA_HOME") or Path.home() / ".ava")
    path = home / "logs" / f"agent-{agent_id}.log"
    rank = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    floor = rank[level.upper()]
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)["record"]
        if rank.get(rec["level"]["name"], 0) < floor:
            continue
        if contains and contains not in rec["message"]:
            continue
        out.append(f'{rec["time"]["repr"]} {rec["level"]["name"]} {rec["message"]}')
    return out[-n:]

print("\n".join(read_log()))                          # last 100 of your own
print("\n".join(read_log(level="WARNING")))           # your warnings+ only
print("\n".join(read_log(agent_id=42, contains="update")))   # peer 42, grep "update"
```

Adapt freely — add a time-window filter on `rec["time"]["repr"]`, regex on the
message, group by `rec["extra"]["process_role"]` (`kernel` vs `subprocess`),
etc. It's just JSONL; do whatever the investigation needs.

---

`decisions/2026-05-09-self-rolling-release.md` is the design doc, `ava/self.py:update` docstring is the SDK contract, and both align with this SKILL — three-way aligned.


## §6 SDK Changelog auto-generation

`reference/generate_sdk_changelog.py` detects agent-visible `ava.*` API changes
between two git refs and writes a Keep a Changelog entry to `SDK_CHANGELOG.md`.

**Two detection strategies, run automatically:**

1. **AST surface diff** — statically parses every public module under `ava/` at
   each ref and diffs the exported symbols: added, removed, renamed
   (heuristic), and caller-breaking signature changes.
2. **Conventional-commit parsing** — scans commits touching `ava/` whose subject
   carries the `!` breaking-change marker or a `BREAKING CHANGE:` trailer.

**When to run:** after cutting a release (daily/weekly via
`scripts/release_cut.py`), or after landing a PR that changes the SDK surface.
The script is idempotent — re-running for the same version label replaces that
label's previous entry instead of appending a duplicate block.

```bash
# Since the latest dated tag through HEAD (--since/--until override the range;
# --stdout previews without updating the file; --version sets a custom label).
.venv/bin/python reference/generate_sdk_changelog.py
```

The script is self-contained (stdlib only — `ast`, `subprocess`, `argparse`,
`dataclasses`) and runs from the repo root. It reads source via `git show`
so no checkout of the target refs is required.
