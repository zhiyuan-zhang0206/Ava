# Ava — minimal code-as-action agent

Small core, minimal by design. One tool (`execute_code`), one namespace (`ava.*`).
[Philosophy →](conventions/philosophy.md)

## Core principles

1. **Small core, minimal** — each layer considered for removal as models improve.
2. **Fail fast** — no fallbacks for model mistakes; use `[]` not `.get()`, explode on unknown enums.
3. **Don't reinvent** — LangGraph, psycopg, uv; swap only when they get in the way.
4. **Single tool** — `execute_code(code: str)` + `ava.*` namespace = all capabilities.
5. **Latest stable** — Python 3.12, Postgres 17, Redis 8.8; no beta/nightly.
6. **English primary** — docs, comments, prompts, error messages in English.

Full elaboration: [`conventions/philosophy.md`](conventions/philosophy.md)

## Output format

One text reply + optional `execute_code` tool call. Lifecycle states: **idle**
(text only), **action** (text + code), **restart** / **terminate** (via `ava.self`).
No multi-tool dispatch, no JSON schema per capability — maximum expressiveness
with no escape hell.
[OKF index →](okf/index.ava.okf.md)

## Stack

| Layer | Choice |
|---|---|
| DB | Postgres 17 |
| Cache | Redis 8.8 |
| Framework | LangGraph (8-node self-looping graph) |
| SDK | `ava` (this repo) |
| Package manager | uv |
| Frontend | Next.js 16 + React 19 + Tailwind 4 + shadcn/ui |

[Frontend OKF →](frontend/frontend.ava.okf.md)

## Running

A **cluster** = one logical deployment; **its identity IS its home path** —
there is no cluster name (display label = home basename, computed on the fly).
Every cluster — including the prod default home `~/.ava` — owns its OWN
Postgres + Redis instance (under its `$AVA_HOME`, on a per-cluster pg/redis port
in its host-port block) **plus a PgBouncer pooler** (default on — `AVA_DB_URL`
points at it; migrations/pg_dump dial the direct URL), so two co-located
clusters share no data plane and cannot cross-talk: isolation is
home-directory isolation, not an identifier kept correct inside one shared
instance. A cluster also owns one outward gateway and
a contiguous host-port block.
The rationale (and the remaining slice 3 — bundling the pg/redis binaries) is in
[`future/infra/embedded-per-cluster-data-plane.md`](future/infra/embedded-per-cluster-data-plane.md).
A **unit** = one install under its own `$AVA_HOME`. A unit carries a SET of
capabilities (`machine_role()` returns a frozenset): `gateway` (owns Postgres/
Redis + the HTTP gateway) and/or `agent-runner` (runs agents + the ops server). A
**single box carries both** (`gateway,agent-runner`, home `~/.ava`) — owns the
data plane and runs agents. Split deployments give each capability its own
machine (a gateway-only `~/.ava_gateway` + a runner-only `~/.ava`).
`install.sh --role gateway,agent-runner|gateway|agent-runner` scaffolds a unit.

Postgres and Redis run as native processes (no Docker — the pg/redis binaries come
from brew on macOS / apt on Linux, but the data plane is driven directly via
`pg_ctl` + `redis-server`, not `brew services`/launchd/systemd). No native Windows
redis exists to drive, so `gateway` is POSIX-only and a **Windows unit carries
`agent-runner` only** ([setup](conventions/windows-setup.md)). Every cluster
brings up its own pair under `$AVA_HOME` (`cli/commands/_cluster_instance.py`:
`initdb` into `$AVA_HOME/pg` — template-cached so a new cluster / a test spins up by
directory copy — plus `redis-server` with a data dir under `$AVA_HOME/redis`, both
on the cluster's own pg/redis port, `requirepass`/scram = the cluster secret).
`ava start` ensures this cluster's instance is up (skip-if-running) and `ava stop`
tears it down (it is private, so it stops even on an internal restart; `ava cluster update`
uses `--keep-infra` to leave it running for the migrate step) — there is no separate
infra verb. Each cluster's database and the Postgres role that owns it share one
identifier, carried by the cluster's `.env` connection URLs **as data** (a fresh
birth writes the fixed `ava`; prod stays on its historical `ava_main` until an
ops rename edits the URLs — nothing re-derives identifiers from a name); the role
is `NOSUPERUSER` owning only its own database. Role + database are provisioned by
that instance's own `initdb` superuser (the OS user) over its private
loopback-`trust` unix socket — no shared bootstrap superuser reaches across
clusters.
The data-plane network posture is uniform — the default is multi-machine, and a
single box is just the special case where the reachable address is loopback (no
single-vs-multi branch). **Auth follows the secret**: with a secret set, each
cluster connects to Postgres as its own role and to redis as its own ACL user
(the identifier its URLs carry), authenticating with `AVA_CLUSTER_SECRET`; the
gateway API + /ops require it as a bearer. An EMPTY secret (single-box default —
off is fully off) serves everything unauthenticated: gateway API and /ops without
auth, pg_hba local+loopback `trust` (no scram lines), redis without requirepass,
every surface loopback-only. The redis instance is single-tenant: its `requirepass` IS the
cluster secret — no separate box-level admin secret. With a secret set,
**pg/redis always bind loopback + this host's reachable address** (`AVA_MACHINE_HOST`,
default `localhost`), de-duplicated — never all interfaces, so the physical LAN
cannot reach the data plane. A single box is reachable only at localhost, so the bind
collapses to loopback alone (zero-config); a split deployment sets each node's real
private-network IP, which is appended, plus the `scram-sha-256` `AVA_TRUSTED_CIDRS`
pg_hba ranges. So a split runner needs reachability *and* the secret. `ava start`
is a consumer of the data plane: it only ensures its instance is up (skip-if-running),
never reconfigures it.

| Path | Role |
|---|---|
| `$AVA_HOME/source/` (default `~/.ava/source/`) | **prod** — cwd of the long-running service sessions; always the default home's cluster (its own pg 5433 / redis 6380 + prod service ports) |
| `~/Ava/` | **dev clone** — worktree dev under `.worktrees/<task>/` (branch from `main`, PR into `main`) (manual / agent-created) or `.claude/worktrees/<task>/` (Claude Code's native worktree tool); each worktree gets its own cluster via `scripts/install.sh --worktree` (home `~/.ava-<worktree-dir>` by default), isolated db/redis/ports/sessions |

Cluster identity is born at **install time** (`scripts/install.sh` →
`python -m cli.install_cluster`): the install allocates the home-keyed registry
record + port block, brings up the cluster's own pg/redis, provisions the
database, writes the cluster `.env` (secret: single-machine = NO-AUTH empty;
gateway-only = minted; `--cluster-secret` wins; existing secrets never rotate),
and — for a worktree — writes the checkout's `.ava_home` pointer. The home is resolved from
the **checkout-anchored** boot (`resolve_ava_home`: `AVA_HOME` env > prod source
→ `~/.ava` > the checkout's `.ava_home` pointer; an env var CONTRADICTING the
checkout's own claim refuses outright unless `AVA_HOME_OVERRIDE=1`), never the
current directory and never a flag — there is no name to pass, so the whole
phantom-cluster incident class (env/cwd/flag naming the wrong cluster) is
structurally gone: the prod `ava` on PATH always acts on `~/.ava`. `ava
start` is a **pure bring-up**: an uninstalled home fails fast pointing at
`install.sh` / `install.sh --worktree` / `ava enroll` by role; so does every
no-target verb acting on this checkout's cluster (`stop`, `restart`, `converge`,
`cluster update|restart|rollback|recover`) — `cluster down|destroy` take `--path`. Cluster
identity is checkout-anchored (never a flag); machine identity
(machine-name / serve-gateway / serve-agent-runner / gateway-url) is
passed on the FIRST start only and persisted to `$AVA_HOME/<field>`
files — later runs need none of them (see `ava start --help`). Registry:
host-level JSON `~/.ava/clusters.json` (`AVA_CLUSTER_REGISTRY`), keyed by home
path. An enrolled agent-runner does NOT birth a cluster; its cluster identity IS
the gateway URL + cluster secret it enrolled with (`ava enroll` materializes the
connection facts; no name travels in the `/api/bootstrap` payload — db/role
identifiers ride inside the URLs as data).

Which cluster an `ava` acts on is fixed by **which checkout it belongs to** (where its `cli` source lives), not by the current directory. `install.sh --role gateway` symlinks the prod checkout's `ava` onto PATH at `~/.local/bin`, so a bare `ava` always means prod; dev work runs `.venv/bin/ava` inside the worktree. Idempotent host wiring (symlink, PATH, `$AVA_HOME` dirs, plugin images) plus each enabled plugin's own `scaffold()` (`setup.py` beside its `plugin.py` — this is how `ava_memory` brings up the memory pool and lays its template down) is applied by the converge phase (`cli/commands/_converge.py`), run on every `ava start` / `ava cluster update` and standalone via `ava converge` (detail in `conventions/runbook.md`). Prod upgrades go through `ava cluster update` (the CLI — the only update entry point; `ava.self.update()` was removed 2026-08), never directly `git checkout` on the prod path.

```bash
scripts/install.sh --role ... | --worktree   # the ONLY birth: registry record + the cluster's
              # own pg/redis + provisioned db + .env (secret: single machine = NO-AUTH empty,
              # gateway-only = minted; serve flags from --role). --worktree births a dev
              # worktree cluster (home ~/.ava-<dir>, --path overrides). Idempotent.
uv sync       # install deps + the `ava` CLI into .venv/bin/
ava start     # pure bring-up: ensures the cluster's own pg/redis instance is up
              # (skip-if-running), then brings up the union of this host's services.
              # Idempotent; takes no identity flags — the home comes from the checkout-
              # anchored boot. An uninstalled home fails fast pointing at install/enroll.
ava stop      # stdin-confirmed force kill (tears down this host's services + this cluster's pg/redis).
              # Leaves the headed browser session running (login Chrome preserved); add
              # --stop-browser to take it down too (a full cluster teardown).
ava status    # check status (includes the pg/redis view)
ava cluster update    # [cluster] upgrade: a gateway-capable host (incl. single box) orchestrates
              # the whole cluster (pause agent-runners -> local pull/sync/migrate/restart -> trigger
              # agent-runner self-updates); a pure agent-runner self-updates (git pull + uv sync + restart)
ava enroll --gateway URL --machine-name NAME --machine-host HOST --cluster-secret S  # join a split-deployment agent-runner to a gateway
              # (presents the cluster secret to the gateway's authenticated /api/bootstrap);
              # materializes the cluster connection facts, then run `ava start`
ava cluster ls                        # list all registered clusters (label = home basename)
ava cluster status                    # full multi-machine roster
ava cluster down --path PATH          # stop the cluster at a home path, keep its slot (data stays on disk)
                                      # (the safe way to stop a dev worktree cluster)
ava cluster destroy --path PATH       # stop a cluster + free its slot + deregister its OS jobs (refused for ~/.ava)
```

Agent processes are **not started directly** — they always go through the gateway via `POST /api/agents` (`ava.agents.spawn` / frontend / `scripts/start_agent.py` all share this one endpoint), so startup ordering has a strict requirement: **start gateway first, then start agents**.

For units/prod/dev paths in depth, the long-running session table, healthchecks,
and the full ops runbook, see
[`conventions/runbook.md`](conventions/runbook.md); dev-host inventory + secret
paths in [`conventions/dev-setup.md`](conventions/dev-setup.md).

**Migrations:** `migrations/YYYYMMDDTHHMMSS_<kebab-name>.sql` (second-precision UTC),
tracked as an applied SET keyed by name — not a sequential integer. `db/schema.sql` is
the squashed **baseline** and the rollback floor. Every post-baseline migration ships a
paired `.down.sql`, and lossy operations (drop column/table, destructive transform) go
**expand-contract** so any one upgrade stays reversible; `scripts/lint_migrations.py`
enforces format + pairing. `rollback_to`/`apply_down`: `future/infra/commit-pinned-cluster.md`.
**Adding a migration:** `.agents/skills/add-a-migration/SKILL.md`.

## Agent instruction files

This `AGENTS.md` is this repo's entry point for all AI coding agents.
`CLAUDE.md` is a symlink → `AGENTS.md`. `frontend/CLAUDE.md` → `frontend/AGENTS.md`. Repo skills live in `.agents/skills/` (open Agent Skills standard); `.ava/skills/` + `.claude/skills/` link back to it, built-ins (Ava Guide, …) link in from `ava_builtins/skills/`.

## Key docs — read on demand

Five axes, one fact per place: `*.ava.okf.md` next to the code = what the system **is**; `decisions/` = **why**
(never rewritten); `future/` = **plans**; `conventions/` = **how to work**; `traces/` = what it **does**
in time — one real recorded run, annotated, every step anchored (write or audit one: `.agents/skills/write-a-trace/`).
[Doc maintenance →](conventions/doc-maintenance.md)

| When you need to… | Read |
|---|---|
| Understand architecture | [`okf/index.ava.okf.md`](okf/index.ava.okf.md) |
| Set up dev environment | [`conventions/dev-setup.md`](conventions/dev-setup.md) |
| Run ops / deploy | [`conventions/runbook.md`](conventions/runbook.md) |
| Write a PR | **[`.agents/skills/write-a-pr-description/SKILL.md`](.agents/skills/write-a-pr-description/SKILL.md)** |
| Understand part of the codebase interactively | [`ava_builtins/skills/ava-workflow/calibrate/SKILL.md`](ava_builtins/skills/ava-workflow/calibrate/SKILL.md) |
| Follow coding conventions | [`conventions/python-conventions.md`](conventions/python-conventions.md) |
| Write SDK docstrings | [`conventions/sdk-docstring-discipline.md`](conventions/sdk-docstring-discipline.md) |
| Maintain docs | [`conventions/doc-maintenance.md`](conventions/doc-maintenance.md) |
| Know what NOT to do | [`conventions/non-goals.md`](conventions/non-goals.md) |
| OKF index | [`okf/index.ava.okf.md`](okf/index.ava.okf.md) (domain overviews + the node graph) |

## Change discipline

1. **Minimal change — but not minimal-only.** Smallest diff that does the job;
   code that works yet hurts to change is a refactoring signal, not a reason to
   live with it.
2. **Refactoring is legitimate work** — in small steps, test-backed, never
   bundled into an unrelated large change.
3. **Scope discipline** — do not expand the task. An unrelated problem of the
   same kind found along the way is fixed in the same PR when it is a small
   leftover (user ruling); otherwise it must leave a trace: report the debt or
   hand it off — never let it evaporate.
4. **Unclear requirements — ask first** ([workflow align](ava_builtins/skills/ava-workflow/align/SKILL.md)).
5. **Behavior changes are locked by a test** — no tests for the sake of tests.
6. **Re-read the diff before committing**; drop what is not necessary.
7. **No new dependencies or upgrades unless necessary.**

Rules 1–2 and 5–7 are referenced, not restated, by
[serious-engineering implementation](.agents/skills/ava-serious-engineering/practices/implementation/SKILL.md),
[serious-engineering dependency-management](.agents/skills/ava-serious-engineering/principles/dependency-management/SKILL.md),
and the `ava.skills.ava-code:testing` discipline; rule 4's ask-first loop is [workflow align](ava_builtins/skills/ava-workflow/align/SKILL.md).

## Workflow (mandatory)

- **Worktree + PR** — every change in `git worktree add -b ava-<id>-<task>`, merged via PR through the **Mergify merge queue** (`.mergify.yml`: auto-rebase onto latest main, CI re-run on the rebased head, auto-merge via rebase — linear history). Direct push forbidden. [Merge queue workflow →](.agents/skills/ship-a-change/SKILL.md)
- **PR description** — must have file-tree diff with ★ critical paths + prose data flow. [Spec →](.agents/skills/write-a-pr-description/SKILL.md)
- **Tech-debt sweeps** — follow `.agents/skills/ava-sweeper/` (debt classes + tracker; boundary vs. lint in [`conventions/lint-vs-sweeper.md`](conventions/lint-vs-sweeper.md)).
- **Complexity analysis** — McCabe cyclomatic complexity + maintainability index via radon, ranked for refactoring. [Skill →](.agents/skills/measure-complexity/SKILL.md)
- **Local tests before push** — run pytest (Python) + vitest/eslint/tsc (frontend) for touched areas before pushing; CI runs the full suite. [How to →](.agents/skills/run-local-tests/SKILL.md)
- **CI to green, then enqueue, then clean up** — after opening a PR poll `.venv/bin/python scripts/ci_utils.py <PR#>` (every 30–60 s, or launch a watcher) until all-green, fixing red immediately rather than letting a failing PR sit; `NO_WORKFLOW_RUNS` means the suite never ran and is **not** green. Then enqueue: `.venv/bin/python scripts/ci_utils.py <PR#> --wait --merge` (posts `@mergifyio queue` on the PR). Mergify rebases the PR onto the latest main and re-runs CI on the rebased head before landing it — **no manual rebase + re-poll loop, no merge-base check**: the queue re-verifies on the tree that actually lands. A PR with conflicts still needs a manual `git rebase origin/main` first. PRs awaiting user review are never enqueued. After merge, remove the local worktree (`git worktree remove <path>`) and delete the remote branch (`git push origin --delete <branch>`) — Mergify does not auto-delete it. [Detail →](.agents/skills/ship-a-change/SKILL.md)
- **Commit = code + docs stable** — docs go in same PR. Structure changes reconcile the co-located `*.ava.okf.md`; scan `conventions/` + `future/` for stale refs.

## Python conventions (quick reference)

- No `if TYPE_CHECKING:` (lint-enforced). Exceptions in `_TYPE_CHECKING_ALLOWED`.
- Per-file line budget: 500 soft / 800 hard.
- No `print()` in framework code (use `shared.log.logger`).
- No decorative emoji in core Python.
- Import layering: `shared < ava < agent < gateway < cli`.
[Full conventions →](conventions/python-conventions.md)

## Communicating with the user

- No dev time estimates. Scope + trade-offs only.
- Describe current behavior; skip "used to be X, then Y" unless forwarding to `decisions/`.
- Clean residual old-API mentions in code/docs when found.
- Candidate next steps: list work options only — no "take a break" wrap-up suggestions.
[Full guide →](conventions/communicating-with-user.md)
