# Ava — minimal code-as-action agent

Small core, minimal by design. One tool (`execute_code`), one namespace (`ava.*`).
[Philosophy →](conventions/philosophy.md)

## Core principles

1. **Small core, minimal** — each layer considered for removal as models improve.
2. **Fail fast** — no fallbacks for model mistakes; use `[]` not `.get()`, explode on unknown enums.
3. **Don't reinvent** — LangGraph, psycopg, uv; swap only when they get in the way.
4. **Single tool** — `execute_code(code: str)` + `ava.*` namespace = all capabilities.
5. **Approved stable** — Python 3.12, Postgres 17, Redis 8.2; upgrades require manual approval; no beta/nightly.
6. **English only — no raw CJK** — docs, comments, prompts, error messages
   in English; the only exemption is frontend i18n locale files (user ruling
   2026-08-27, enforced repo-wide by `scripts/lint_no_cjk.py`).

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
| Cache | Redis 8.2 |
| Framework | LangGraph (8-node self-looping graph) |
| SDK | `ava` (this repo) |
| Package manager | uv |
| Frontend | Next.js 16 + React 19 + Tailwind 4 + shadcn/ui |

[Frontend OKF →](ui/web/web.ava.okf.md)

## Running

A **cluster** = one logical deployment; **its identity IS its home path** — no
cluster name (display label = home basename). A **unit** = one install under its
own `$AVA_HOME`, carrying a capability set: `gateway` (owns Postgres/Redis + the
HTTP gateway) and/or `agent-runner` (agents + the ops server) and/or
`observability-station` (owns the native LGTM observability backends — the
declarative form of the `$AVA_HOME/lgtm-host` marker); a single box carries
both gateway and agent-runner (home `~/.ava`). Every cluster owns its OWN Postgres + Redis
instance under `$AVA_HOME` (per-cluster ports in its host-port block) **plus a
PgBouncer pooler** (default on — `AVA_DB_URL` points at it; migrations/pg_dump
dial the direct URL); isolation is home-directory isolation, so co-located
clusters share no data plane. The data plane is **swappable**: URLs naming a
foreign host (another machine or a SaaS provider) make the cluster treat it as
remote-managed — local instance bring-up/stop/ACL/pooler management are skipped
and degrade to reachability probes (see
`docs/history/2026-08-28/connection-layer-swappable.md`). Postgres
and Redis run as native processes (no Docker); `gateway` is POSIX-only, so a
Windows unit carries `agent-runner` only
([setup](conventions/windows-setup.md)). Rationale + the remaining slice:
[`future/infra/embedded-per-cluster-data-plane.md`](future/infra/embedded-per-cluster-data-plane.md).

**Auth follows the authority boundary.** `AVA_CLUSTER_SECRET` is the
control-plane bearer for the gateway API, `/ops`, bootstrap, and machine
registration. The gateway alone holds the independent Postgres owner password
(`AVA_DB_ADMIN_PASSWORD`) and Redis default-user password
(`AVA_REDIS_ADMIN_PASSWORD`); agents receive only the runner DB projection and
the Redis ACL credential embedded in `AVA_REDIS_URL`. Identity stays data in URL
usernames, never derived from a name. The bearer still decides the network
posture: Postgres and its pooler bind loopback + this host's reachable address
only when set; Redis is always loopback-only with off-box inbound carried by the
host-level relay bridge. An EMPTY secret (single-box default — off is fully off)
keeps every credential empty and serves everything unauthenticated, loopback-only.
Every `AVA_PROCESS_PROFILE=agent` process, including the single-box hosted
agent-host, is launched with an explicit `ava_runner` DB URL projection; it must
never combine an owner username with the cluster bearer.

| Path | Role |
|---|---|
| `$AVA_HOME/source/` (default `~/.ava/source/`) | **prod** — cwd of the long-running service sessions; always the default home's cluster (its own pg 5433 / redis 6380 + prod service ports) |
| `~/Ava/` (this checkout) | **dev clone** — worktree dev under `.worktrees/<task>/` (branch from `main`, PR into `main`) (manual / agent-created) or `.claude/worktrees/<task>/` (Claude Code's native worktree tool); each worktree gets its own cluster via `scripts/install.sh --worktree` (home `~/.ava-<worktree-dir>` by default), isolated db/redis/ports/sessions |

Cluster identity is born at **install time** (`scripts/install.sh` →
`python -m cli.install_cluster`): registry record + port block + the cluster's
own pg/redis + provisioned db + cluster `.env` (single-machine = NO-AUTH empty;
gateway-only = minted bearer plus independent data-plane credentials; existing
credentials never rotate at install). The home
is resolved **checkout-anchored** (`AVA_HOME` env > prod source → `~/.ava` > the
checkout's `.ava_home` pointer; a contradicting env refuses outright unless
`AVA_HOME_OVERRIDE=1`) — never cwd, never a flag. `ava start` is a pure
bring-up: an uninstalled home fails fast pointing at `install.sh` /
`install.sh --worktree` / `ava enroll` by role. Machine identity
(machine-name / serve-gateway / serve-agent-runner / gateway-url) is passed on
the FIRST start only and persisted to `$AVA_HOME/<field>` files. An enrolled
agent-runner does NOT birth a cluster; its identity IS the gateway URL +
cluster secret it enrolled with. Registry: host-level JSON
`~/.ava/clusters.json`, keyed by home path.

Which cluster an `ava` acts on is fixed by **which checkout it belongs to**
(where its `cli` source lives), not by the current directory. `install.sh
--role gateway` symlinks the prod checkout's `ava` onto PATH at
`~/.local/bin`, so a bare `ava` always means prod; dev work runs
`.venv/bin/ava` inside the worktree. Host wiring + each plugin's `scaffold()`
are applied by the converge phase (`cli/commands/_converge.py`) on every
`ava start` / `ava cluster update` (standalone: `ava converge`). Prod upgrades
go through `ava cluster update` (the CLI — the only update entry point),
never directly `git checkout` on the prod path.

```bash
scripts/install.sh --role ... | --worktree   # the ONLY birth (idempotent): registry record +
              # cluster's own pg/redis + provisioned db + .env (--worktree = dev worktree
              # cluster, home ~/.ava-<dir>). Secret: single machine = NO-AUTH empty,
              # gateway-only = minted.
uv sync       # install deps + the `ava` CLI into .venv/bin/
ava start     # pure bring-up: ensures the cluster's own pg/redis is up, then brings up this
              # host's services. No identity flags — the home comes from the checkout-anchored boot.
ava pause     # normal agent drain; preserves infrastructure, browser and persistent PTYs.
ava stop      # normal agent drain, then full local stop including PTYs/browser/private pg+redis.
              # --keep-infra / --keep-service retain resources; --force is explicit escalation.
              # ava start resumes after readiness; agent identities and durable data survive.
ava status    # check status (includes the pg/redis view)
ava cluster update    # upgrade: a gateway-capable host orchestrates the whole cluster (pause
              # runners -> pull/sync/migrate/restart -> trigger runner self-updates); a pure
              # agent-runner self-updates.
ava enroll --gateway URL --machine-name NAME --machine-host HOST  # join a cluster (export
              # AVA_CLUSTER_SECRET first; verifies the runner projection, then run `ava start`)
ava cluster ls / status             # list all registered clusters (label = home basename) / full multi-machine roster
ava cluster down --path PATH        # stop the cluster at a home path, keep its slot (data stays on disk)
ava cluster destroy --path PATH     # stop + free its slot + deregister its OS jobs (refused for ~/.ava)
```

Agent processes are **not started directly** — they always go through the
gateway via `POST /api/agents` (`ava.agents.spawn` / frontend /
`scripts/start_agent.py` all share this one endpoint): **start gateway first,
then start agents**.

Units/prod/dev paths in depth, the long-running session table, healthchecks,
and the full ops runbook: [`conventions/runbook.md`](conventions/runbook.md);
dev-host inventory + secret paths: [`conventions/dev-setup.md`](conventions/dev-setup.md).

**Migrations:** `migrations/YYYYMMDDTHHMMSS_<kebab-name>.sql` (second-precision UTC),
tracked as an applied SET keyed by name; `db/schema.sql` is the squashed baseline.
Every migration ships a paired `.down.sql`, and lossy operations go
**expand-contract** so any one upgrade stays reversible (`scripts/lint_migrations.py`
enforces format + pairing). **Adding a migration:** `.agents/skills/add-a-migration/SKILL.md`.

## Agent instruction files

This `AGENTS.md` is this repo's entry point for all AI coding agents.
`CLAUDE.md` is a symlink → `AGENTS.md`. `ui/web/CLAUDE.md` → `ui/web/AGENTS.md`. Repo skills live in `.agents/skills/` (open Agent Skills standard); `.ava/skills/` + `.claude/skills/` link back to it, built-ins (Ava Guide, …) link in from `ava_builtins/skills/`.

## Key docs — read on demand

Five axes, one fact per place: `*.ava.okf.md` next to the code = what the system **is**; `decisions/` = **why** (never rewritten); `future/` = **plans**; `conventions/` = **how to work**;
`postmortems/` = **why a failure escaped** — frozen incident narratives, each naming the guardrail it bought, distilled into [`conventions/defensive-patterns.md`](conventions/defensive-patterns.md)
(read before lifecycle / release / infra work). What the system **does in time** is not an axis — no run is committed; query the live one ([`.agents/skills/inspect-a-trace/`](.agents/skills/inspect-a-trace/SKILL.md)).
[Doc maintenance →](conventions/doc-maintenance.md)

| When you need to… | Read |
|---|---|
| Understand architecture | [`okf/index.ava.okf.md`](okf/index.ava.okf.md) (domain overviews + the node graph) |
| Set up dev environment | [`conventions/dev-setup.md`](conventions/dev-setup.md) |
| Run ops / deploy | [`conventions/runbook.md`](conventions/runbook.md) |
| Write a PR | **[`.agents/skills/write-a-pr-description/SKILL.md`](.agents/skills/write-a-pr-description/SKILL.md)** |
| Understand part of the codebase interactively | [`ava_builtins/skills/ava-workflow/calibrate/SKILL.md`](ava_builtins/skills/ava-workflow/calibrate/SKILL.md) |
| Follow coding conventions | [`conventions/python-conventions.md`](conventions/python-conventions.md) |
| Write SDK docstrings | [`conventions/sdk-docstring-discipline.md`](conventions/sdk-docstring-discipline.md) |
| Maintain docs | [`conventions/doc-maintenance.md`](conventions/doc-maintenance.md) |
| Know what NOT to do | [`conventions/non-goals.md`](conventions/non-goals.md) |
| Avoid a bug class that already bit us | [`conventions/defensive-patterns.md`](conventions/defensive-patterns.md) (stories behind it: `postmortems/`) |

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
[serious-engineering implementation](ava_builtins/skills/ava-serious-engineering/practices/implementation/SKILL.md),
[serious-engineering dependency-management](ava_builtins/skills/ava-serious-engineering/principles/dependency-management/SKILL.md),
and the `ava.skills.ava-code:testing` discipline; rule 4's ask-first loop is [workflow align](ava_builtins/skills/ava-workflow/align/SKILL.md).

## Workflow (mandatory)

- **Worktree + PR** — every change in `git worktree add -b ava-<id>-<task>`, merged via PR through the Trunk merge queue; direct push forbidden. Merge is not deployment; runtime rollout requires separate operator authorization and verification. [Workflow →](.agents/skills/ship-a-change/SKILL.md)
- **PR description** — must have file-tree diff with ★ critical paths + prose data flow. [Spec →](.agents/skills/write-a-pr-description/SKILL.md)
- **Tech-debt sweeps** — follow `.agents/skills/ava-sweeper/` (debt classes + tracker; boundary vs. lint in [`conventions/lint-vs-sweeper.md`](conventions/lint-vs-sweeper.md)).
- **Complexity analysis** — McCabe cyclomatic complexity + maintainability index via radon, ranked for refactoring. [Skill →](.agents/skills/measure-complexity/SKILL.md)
- **Local tests before push** — run pytest (Python) + vitest/eslint/tsc (frontend) for touched areas before pushing; CI runs the full suite. [How to →](.agents/skills/run-local-tests/SKILL.md)
- **CI to green, then enqueue, then clean up** — poll `.venv/bin/python scripts/ci_utils.py <PR#>` until all-green (fix red immediately; `NO_WORKFLOW_RUNS` = the suite never ran = not green), then submit with `--wait --merge` (submits to the Trunk merge queue; the queue verifies the combined tree that actually lands — a PR with conflicts still needs a manual `git rebase origin/main` first; PRs awaiting user review are never enqueued). After merge: remove the local worktree and delete the remote branch. [Detail →](.agents/skills/ship-a-change/SKILL.md)
- **Commit = code + docs stable** — docs go in same PR. Structure changes reconcile the co-located `*.ava.okf.md`; scan `conventions/` + `future/` for stale refs.

## Python conventions (quick reference)

- No `if TYPE_CHECKING:` (lint-enforced). Exceptions in `_TYPE_CHECKING_ALLOWED`.
- Per-file line budget: 600 soft / 800 hard.
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
