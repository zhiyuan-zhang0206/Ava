# Ava — minimal code-as-action agent

Small core, minimal by design. One tool (`execute_code`), one namespace (`ava.*`).
[Philosophy →](conventions/philosophy.md)

## Core principles

1. **Small core, minimal** — each layer considered for removal as models improve.
2. **Fail fast** — no fallbacks for model mistakes; use `[]` not `.get()`, explode on unknown enums.
3. **Don't reinvent** — LangGraph, psycopg, uv; swap only when they get in the way ([how to choose](conventions/technology-selection.md)).
4. **Single tool** — `execute_code(code: str)` + `ava.*` namespace = all capabilities.
5. **Approved stable** — Python 3.12, Postgres 17, Redis 8.2; upgrades require manual approval; no beta/nightly.
6. **English only — no raw CJK** — docs, comments, prompts, error messages
   in English; the only exemption is frontend i18n locale files (user ruling
   2026-08-27, enforced repo-wide by `scripts/content_lint/lint_no_cjk.py`).

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

[Frontend OKF →](ui/web/docs/web.ava.okf.md)

## Running

A **cluster** = one logical deployment; **its identity IS its home path** — no
cluster name (display label = home basename). A **unit** = one install under its
own `$AVA_HOME`, carrying a capability set: `gateway` (owns Postgres/Redis + the
HTTP gateway) and/or `agent-runner` (agents + the ops server) and/or
`observability-station` (owns the native LGTM observability backends — the
declarative form of the `$AVA_HOME/lgtm-host` marker); a single box carries
both gateway and agent-runner (home `~/.ava`). Every cluster owns its OWN Postgres + Redis
instance under `$AVA_HOME` (on the fixed port table, `base/host/env/port_table.py`) **plus a
PgBouncer pooler** (default on — `AVA_DB_URL` points at it; migrations/pg_dump
dial the direct URL); isolation is home-directory isolation, so co-located
clusters share no data plane. The data plane is **swappable**: URLs naming a
foreign host (another machine or a SaaS provider) make the cluster treat it as
remote-managed — local instance bring-up/stop/ACL/pooler management are skipped
and degrade to reachability probes (see
`decisions/2026-08-28-connection-layer-swappable.md`). Postgres
and Redis run as native processes (no Docker); `gateway` is POSIX-only, so a
Windows unit carries `agent-runner` only
([setup](conventions/windows-setup.md)). Rationale + the remaining slice:
[`future/infra/embedded-per-cluster-data-plane.md`](future/infra/embedded-per-cluster-data-plane.md).

**Auth follows the authority boundary.** `AVA_CLUSTER_SECRET` is the gateway's human bearer (API,
frontend login); it stays on the gateway and rotates only explicitly
(`scripts/data_plane_ops/rotate_cluster_secret.py`; backups use a birth-pinned passphrase). An EMPTY secret
(single-box default) leaves the API, `/ops` and frontend unauthenticated and binds every data-plane
listener to loopback; a set secret adds this host's reachable address for Postgres, its pooler and
Linux Redis (macOS Redis stays loopback-only, off-box inbound via the relay bridge). The internal data plane always
authenticates: Postgres and PgBouncer admit only SCRAM application logins (the OS-user administrator
and the collector's password-less monitoring role use `peer` on the owner-only socket), and Redis
requires its generated passwords. Application processes never hold schema-owner or admin credentials:
the owner is NOLOGIN, and the write generation — one gateway and one runner login
inheriting the NOLOGIN groups `ava_gateway` / `ava_runner`, plus one machine API token per class,
recorded in `$AVA_HOME/db-authority/` — is delivered in the launch environment of the admitted runtime
(`AVA_DB_URL`, `AVA_API_TOKEN`) and, at 0600, in `$AVA_HOME/run/ava-root/manifests.json`, which the
next start rewrites; `.env` holds the credential-free endpoint. Machine
callers present their API token (an agent or runner process without it fails, never falling back to the secret):
the gateway admits the active generation's tokens (never a pending one), an ops server its generation's two.
Bootstrap serves configuration only: a remote agent-runner gets its runner login, API and telemetry tokens in a
sealed bundle its start installs (`ava cluster db-authority issue-unit`), all shared across runner units, and
never holds the human secret. A home born before this model (no ledger) is refused; no conversion exists.

| Path | Role |
|---|---|
| `$AVA_HOME/source/` (default `~/.ava/source/`) | **prod** — cwd of the long-running service sessions; always the default home's cluster (its own pg 5433 / redis 6380 + prod service ports) |
| `~/Ava/` (this checkout) | **dev clone** — worktree dev under `.worktrees/<task>/` (branch from `main`, PR into `main`) (made by `scripts/setup-worktree.sh`) or `.claude/worktrees/<task>/` (Claude Code's native worktree tool); a worktree owns no cluster: it verifies with selected tests and CI, and a process tree that imports application code sets its own temporary `AVA_HOME` |

`ava init` initializes a home once and starts nothing: Settings-free, it persists
`start-intent.json` (home, capabilities, admitted checkout, ports; credentials until
`.env` holds them) and publishes `.env`. An interrupted init resumes (`ava init`, no
flags); an initialized home refuses a second one. `ava start` admits only an
initialized home, creates its data plane on the first start, and retains the service
selection on repeats. The home is `AVA_HOME`
when set, else `~/.ava`, read whenever it is needed — production does not depend
on the variable; a test session and a git hook set it in code; a dev tool that imports application code
sets it once at its top and every descendant inherits it. A home that carries its
own `<home>/source` checkout is operated only by that checkout's CLI: any other
checkout refuses every command (a dev CLI names a temporary `AVA_HOME`).

`ava init` takes the machine name, capability flags and reachable host. A
remote agent-runner joins through it with `--gateway-url` and its
capability bundle (`--db-capability`, the transport key in `AVA_DB_CAPABILITY_KEY`),
which also authenticates it; it creates no gateway or local data plane. A later bundle
(a fresh expiry, a rotated telemetry token) goes to `ava cluster db-authority install-unit`.
A home describes only itself: its ports live in its own start intent, and no host
file lists clusters; the state a host shares (vendored runtime, initdb template,
PTY freeze, coding-session owners) lives in the home too. Initial
configuration may come from `--config-file`; credentials and identity survive an interrupted init.

Application processes have one supervisor: `ava-root`. On macOS the ancestry
is `launchd -> signed permissions helper -> ava-root -> services / agent-host`.
On Linux it is `systemd/direct launch -> ava-root -> services / agent-host`;
Linux has no helper layer. Root names the supervisor, not a privileged user.
Native data-plane custody is separate so application shutdown can retain the
database for migrations. Readiness requires a real protocol response from the
captured process generation; missing evidence cannot become success.

A checkout's own `.venv/bin/ava` runs **the checkout it belongs to** (where its `cli`
source lives), not the current directory, against the home above; the first `ava init` runs it. The host's bare `ava`
(`~/.local/bin/ava`, linked by production start converge) is a plain link to the production
checkout's `.venv/bin/ava`; which home it acts on is `AVA_HOME`, else `~/.ava`, like every CLI.
OS jobs (launchd, crontab, the Linux boot unit) are named for the job, not the home, and only
the default home registers or removes them. Host wiring + each plugin's `scaffold()` are
applied by the source-start converge phase (`cli/commands/converge/host.py`; standalone:
`ava converge`).

Every unit runs from its own `$AVA_HOME/source` checkout (source mode). A
networked cluster is updated by stopping every unit, switching every checkout and
starting again, scripted as `python -m cli.fleet_update down` and `up`
([runbook](conventions/runbook.md#updating-a-networked-cluster-in-source-mode)).

```bash
uv sync       # prepare the checkout dependencies and CLI; no cluster is created
ava init --serve-gateway --serve-agent-runner --machine-name NAME
              # record this home's identity once; starts nothing (an initialized home is refused)
ava start     # first start creates the data plane; later ones reconcile the desired service roster
              # --only-service NAME is an allowlist; --disable-service NAME is an exclusion
              # --all-services explicitly resets selection; omitted flags retain it
ava stop      # normal agent drain, then full local stop including PTYs/browser/private pg+redis.
              # --keep-infra / --keep-service retain resources; --force is explicit escalation.
              # ava start resumes after readiness; agent identities and durable data survive.
ava restart   # stop then start in one command; keeps private pg+redis and the browser, closes terminals.
ava status    # check status (includes the pg/redis view)
ava cluster db-authority issue-unit --machine NAME --home UNIT_HOME --out BUNDLE
              # gateway: seal one unit's capability; prints its transport key once
ava init --no-serve-gateway --serve-agent-runner --gateway-url URL --machine-name NAME --machine-host HOST --db-capability BUNDLE
              # join a remote runner; supply AVA_DB_CAPABILITY_KEY in the
              # environment; the bundle is consumed
ava cluster db-authority install-unit BUNDLE
              # an initialized runner: install a newer bundle (stop, install-unit, start)
ava cluster status                 # full multi-machine roster of this cluster
ava cluster destroy [--drop-db]    # decommission this host's cluster: stop, deregister its OS jobs and
                                   # helper, mark the home detached; you type the home path at a
                                   # terminal, and there is no flag that skips that
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
There are no down migrations: a mistake is fixed forward, lossy operations go
**expand-contract**, and a migration already on main is never edited, deleted or renamed
(`scripts/content_lint/lint_migrations.py` enforces it). **Adding a migration:** [database migration guide](db/docs/migrations.md).

## Agent instruction files

This `AGENTS.md` is this repo's entry point for all AI coding agents; Claude Code reads it (and `ui/web/AGENTS.md`) directly, so there is no `CLAUDE.md`.
Repo skills live in `.agents/skills/` (open Agent Skills standard); `.ava/skills/` + `.claude/skills/` link back to it, built-ins (Ava Guide, …) link in from `ava_builtins/skills/`.

## Key docs — read on demand

Five axes, one fact per place: `*.ava.okf.md` in each package's `docs/` = what the system **is**; `decisions/` = **why** (never rewritten); `future/` = **plans**; `conventions/` = **how to work**;
`postmortems/` = **why a failure escaped** — frozen incident narratives, each naming the guardrail it bought, distilled into [`conventions/defensive-patterns.md`](conventions/defensive-patterns.md)
(read before lifecycle / release / infra work). What the system **does in time** is not an axis — no run is committed; query the live one ([`.agents/skills/inspect-a-trace/`](.agents/skills/inspect-a-trace/SKILL.md)).
[Doc maintenance →](conventions/doc-maintenance.md)

| When you need to… | Read |
|---|---|
| Understand architecture | [`okf/index.ava.okf.md`](okf/index.ava.okf.md) (domain overviews + the node graph) |
| Set up dev environment | [`conventions/dev-setup.md`](conventions/dev-setup.md) |
| Run ops / deploy | [`conventions/runbook.md`](conventions/runbook.md) |
| Write a PR | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Understand part of the codebase interactively | [`ava_builtins/skills/practice/ava-workflow/calibrate/SKILL.md`](ava_builtins/skills/practice/ava-workflow/calibrate/SKILL.md) |
| Find every reference before changing or moving something | `.venv/bin/python scripts/audit/where_used.py TARGET` (`pkg.mod:name`, `pkg.mod` or a path): importers, tests, patch targets, docs, baselines in one call; after a move, `scripts/audit/module_moves.py OLD=NEW` |
| Follow coding conventions | [`conventions/python-conventions.md`](conventions/python-conventions.md) |
| Know which layer may import which | [`conventions/import-layering.md`](conventions/import-layering.md) |
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
4. **Unclear requirements — ask first** ([workflow align](ava_builtins/skills/practice/ava-workflow/align/SKILL.md)).
5. **Behavior changes are locked by a test** — no tests for the sake of tests.
6. **Re-read the diff before committing**; drop what is not necessary.
7. **Price our own code's liabilities together with new dependencies.**

Rules 1–2 and 5–7 are referenced, not restated, by
[serious-engineering implementation](ava_builtins/skills/practice/ava-serious-engineering/practices/implementation/SKILL.md)
and [serious-engineering dependency-management](ava_builtins/skills/practice/ava-serious-engineering/principles/dependency-management/SKILL.md);
rule 4's ask-first loop is [workflow align](ava_builtins/skills/practice/ava-workflow/align/SKILL.md).

## Workflow (mandatory)

- **Worktree + PR** — every change in a worktree made by `bash scripts/setup-worktree.sh <task>` (run from the main clone or any worktree: branch `ava-<task>` off fresh `origin/main`, its own real `.venv`, locked install, `npm ci` for `ui/web`, hook / editable-venv checks), then `cd` to the path on its last line `worktree ready: <path> (branch <branch>)`. Never `git worktree add` by hand or symlink a `.venv`; a worktree from Claude Code's own tool gets the same script with no argument, run inside it. Merged via PR through the Trunk merge queue; direct push forbidden. Merge is not deployment; runtime rollout requires separate operator authorization and verification. [Workflow →](.agents/skills/ship-a-change/SKILL.md)
- **PR description** — must have file-tree diff with ★ critical paths + prose data flow. [Contribution guide →](CONTRIBUTING.md)
- **Tech-debt sweeps** — follow `.agents/skills/ava-sweeper/` (debt classes + tracker; boundary vs. lint in [`conventions/lint-vs-sweeper.md`](conventions/lint-vs-sweeper.md)).
- **Complexity analysis** — McCabe cyclomatic complexity + maintainability index via radon, ranked for refactoring. [Skill →](.agents/skills/measure-complexity/SKILL.md)
- **Local tests before push** — local checks cover **only what you changed**: `.venv/bin/pytest -n 2 <files>` on the test files you changed plus those that execute your changed code; pyright on your changed files only (`git diff --name-only --diff-filter=ACMR -z origin/main...HEAD -- '*.py' | xargs -0 -r .venv/bin/pyright`); UI eslint and vitest on the changed paths, `tsc --noEmit` once after the last UI edit.
  **Never** run `pyright` or `pytest` without file arguments, on a whole directory, or as any full suite locally — including for `base/` changes; full suites run in CI only (user ruling 2026-09-22; pyright included 2026-10-01). Do not re-run a check when no code changed since its last run. [How to →](conventions/testing.md)
- **Git hooks** — install both stages from the main clone's stable `.venv`; commit hooks judge only the change, heavy static checks run at pre-push. [Install and guardrails →](conventions/runbook.md#git-hooks-pre-commit--pre-push)
- **CI to green, then enqueue, then clean up** — poll `.venv/bin/python scripts/ci_utils.py <PR#>` until all-green (fix red immediately; `NO_WORKFLOW_RUNS` = the suite never ran = not green), then submit with `--wait --merge` (submits to the Trunk merge queue; the queue verifies the combined tree that actually lands — a PR with conflicts still needs a manual `git rebase origin/main` first; PRs awaiting user review are never enqueued). After merge: remove the local worktree and delete the remote branch. [Detail →](.agents/skills/ship-a-change/SKILL.md)
- **Commit = code + docs stable** — docs go in same PR. Structure changes reconcile the package's `docs/` OKF nodes; scan `conventions/` + `future/` for stale refs.

## Python conventions (quick reference)

- No `if TYPE_CHECKING:` (lint-enforced). Exceptions in `_TYPE_CHECKING_ALLOWED`.
- Structure budgets: ≤800 lines per `.py`; ≤20 direct Python files/subdirectories per directory; function cc <15 (10–14 warn), nesting ≤5; packages + tests/scripts, frozen shrink-only baseline. Locality: no `_`-private import from outside its owning package; single-owner decisions (Postgres dial → `base/db/connections.py`); no path imports under `ava_builtins/` (a within-skill `__file__` guard excepted); inject what is read to decide, write-only facades may stay global, background work is a service loop, never a free-floating `create_task` — all frozen in the same baseline.
- No `print()` in framework code (use `base.log.logger`); no decorative emoji in core Python.
[Full conventions →](conventions/python-conventions.md)

## Communicating with the user

- No dev time estimates. Scope + trade-offs only.
- Describe current behavior; skip "used to be X, then Y" unless forwarding to `decisions/`.
- Clean residual old-API mentions in code/docs when found.
- Candidate next steps: list work options only — no "take a break" wrap-up suggestions.
[Full guide →](conventions/communicating-with-user.md)
