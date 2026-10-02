---
name: run-local-tests
description: Runs the Ava repo's Python, frontend, and end-to-end checks and diagnoses wedged local test infrastructure. Use before pushing any code change, or when pytest, initdb, Postgres, or cleanup processes will not start normally.
---

# Run local tests

## Test layering

- **Local checks cover only what you changed** (user ruling 2026-09-22; pyright
  included 2026-10-01). Whole-repository runs saturate the shared dev host and
  duplicate what CI runs on every PR, so they belong to CI alone:
  - **Never** run `pyright` without file arguments, `pytest` without file
    arguments, `pytest tests/` or any other whole directory, or any full suite
    (backend or frontend) locally — including after a `base/` change. Widen by
    dependency (see "Pick targeted tests by dependency" below), never by
    directory.
  - **pytest**: the test files you added or changed, plus the test files that
    execute the code you changed, and nothing else — always with `-n 2` for
    parallelism. The test is relevance, not a count: the changed module's own
    tests, then the direct consumers that assert over what you touched. Anything
    that does not bear on your change is CI's job.
  - **pyright**: only the `.py` files you changed (command below).
  - **UI**: eslint and vitest on the changed paths. `tsc --noEmit` is
    project-wide by nature, so run it once after your last UI edit.
  - **Never repeat a check on unchanged code.** Re-run a check only after you
    edited something it covers; after a failure, fix and re-run just the failing
    files. A second identical run with no edit in between tells you nothing.
- **Commit hooks** judge what the commit changes: the per-file lints take the
  changed files, a whole-project check runs only when one of its inputs changed,
  and ESLint lints the changed frontend files. They run no tests. For targeted
  local verification, skip the full-suite `frontend-vitest` pre-push hook by name
  and record the targeted tests run instead; do not bypass the other hooks.
- **Local tests before push** — mandatory. After commit and before `git push`,
  run the checks above for the areas you touched:
  - Python tests: `.venv/bin/pytest -n 2 <selected-test-files-or-node-ids>`.
  - Python types: pyright on the branch's changed files only. The pipeline
    handles an empty list (no `.py` change: nothing runs, exit 0), deleted
    files (`--diff-filter=ACMR`) and paths with spaces (`-z` / `-0`):

    ```bash
    git diff --name-only --diff-filter=ACMR -z origin/main...HEAD -- '*.py' \
      | xargs -0 -r .venv/bin/pyright
    ```

    Before committing, this variant also sees uncommitted and new files:

    ```bash
    { git diff --name-only --diff-filter=ACMR -z "$(git merge-base origin/main HEAD)" -- '*.py'
      git ls-files -z --others --exclude-standard -- '*.py'; } | xargs -0 -r .venv/bin/pyright
    ```
  - Frontend, from `cd ui/web`: `npx vitest related --run <changed-source-files>`
    (every test that imports them; or `npx vitest run <selected-test-files>`),
    `npx eslint <changed-files>`, then `npx next typegen` and
    `npx tsc --noEmit` once.
  Failures must be fixed before pushing; do not rely on CI to catch them.
  A new test must be **shown to fail without the fix** — run it against the
  stashed pre-change code, or invert its assertion momentarily.
- **A fresh worktree's `.venv` must be its own real directory — never a
  symlink to a shared venv.** `ln -s ~/Ava/.venv .venv` looks convenient,
  but a later `uv sync` in that worktree writes through the symlink and
  re-points the shared venv's editable `.pth` here — breaking every other
  checkout that uses that venv
  ([rationale](../../../conventions/dev-setup.md#development-in-a-worktree)).
  No venv yet? Run `bash scripts/setup-worktree.sh` inside the worktree (it
  builds the worktree's own), or for a test-only run reuse another worktree's real venv:
  `PYTHONPATH=<this-worktree> <other-worktree>/.venv/bin/python -m pytest ...`
  (PYTHONPATH outranks that venv's `.pth`, so the tests run against this
  worktree's code). This shortcut is only valid when every tested subprocess
  preserves that import path. Python `-I` ignores `PYTHONPATH` and imports the
  reused venv's editable checkout. Tests that launch isolated Python children
  need this worktree's own real venv. Verify the child import with
  `.venv/bin/python -I -c 'import agent; print(agent.__file__)'` before trusting
  a cross-process result.
- **Native lifecycle proof uses the release interpreter build.** Matching only
  Python's major/minor version is insufficient: managed builds can omit optional
  OS bindings exposed by a distribution Python. Record the exact interpreter,
  build and native capabilities; do not substitute a system interpreter to turn
  a failed release-environment test green. Exercise cleanup as well as startup.
- **Every worktree `uv` command needs `env -u VIRTUAL_ENV`**, including
  `uv run` and `uv pip`. Never run bare `uv pip install`: it can target an
  inherited shared production environment and remove its launcher (#4629).
- **Pick targeted tests by dependency, not by directory.** Shared changes
  can break consumer-side enum or field-set assertions. Locate those consumers
  and include their specific test files locally, rather than expanding to a
  directory or the full backend suite. `.venv/bin/python scripts/audit/where_used.py
  <module-or-symbol>` lists them (its TESTS group) with every other reference in
  one call. For a new enum member,
  search with
  `rg 'set\(<EnumName>\)|list\(<EnumName>\)' -g '**/tests/**'`. CI must still run the full
  suite before merge; an unrun or skipped CI suite is not a pass.
  ([postmortem](../../../postmortems/0003-touched-areas-is-not-the-blast-radius.md))

- **CI independently enforces local checks** except the warn-only hook-installation
  check: `backend-structure` runs structural lints and conditional explicit
  codegen hooks; `backend-static` and `frontend` own the heavy checks directly.
  A markdown-only PR (which skips backend under the change classifier) still gets
  the doc-lint family from the classify-independent `doc-lints` job. That
  redundancy is what makes `SKIP=` safe and `--no-verify` merely invisible
  rather than actually permissive.
- **The reverse is not symmetric, deliberately.** A few CI steps have no local
  hook because they need a toolchain a dev machine may not have:
  `scripts/ci/migration_smoke.py` boots a throwaway Postgres and shells out to
  `psql`. A hook that fails for reasons unrelated to your commit is what breeds
  the `--no-verify` habit, so it stays CI-only. The cheap half of the migration
  gate (`scripts/content_lint/lint_migrations.py` — filename format, up/down pairing,
  baseline seed) *is* a local hook, gated on `migrations/` + `db/schema.sql`.
- **Positional test paths run grouped by directory, whatever order you pass.**
  pytest 9 hides a conftest's fixtures (autouse ones included) from any directory
  it collects twice, which happens when the paths leave a directory and come back
  (`tests/agent/a.py tests/b.py tests/agent/c.py`). `tests/fixtures/collection_guard.py`
  sorts the paths before collection and stops a run whose collection still splits
  a directory.
- **Full non-e2e + e2e + coverage threshold runs in CI** — it's the merge gate.
- **Framework pre-push hooks** run pyright (scoped to the branch's own changed
  `.py` files, same local-only-changed-files rule as above) and, on frontend
  changes, tsc, whole-project eslint and vitest. They also rerun the pre-commit
  stage over the branch diff, re-check the generated-artifact hooks whose inputs
  the branch deleted, and scan every test for patch targets when the branch
  touches Python (none of this runs pytest). Install
  both stages from the main clone's stable `.venv`, never a worktree:
  `.venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-push`.
  See [the hook runbook](../../../conventions/runbook.md#git-hooks-pre-commit--pre-push)
  for shared-machine guards and warn-only installation checks.

## Two rules for the tests themselves

Both are guardrails from real escapes; the rules are condensed in
[`conventions/defensive-patterns.md`](../../../conventions/defensive-patterns.md).

- **A guard only guards if the regression actually fails it.** When you add a
  protective test, lint, or assertion, introduce the regression it targets, watch
  it go red, and revert — in the same PR. For a test written against a fix that
  already landed: `git checkout <sha-before-the-fix> -- <file>`, re-run, confirm
  *that* test fails, `git checkout HEAD -- <file>`. **Not `git stash push
  <file>`** — that stashes only uncommitted changes, so against a committed fix
  it stashes nothing and the test passes, which looks exactly like a proof and is
  the opposite of one. Confirm the revert landed before trusting the result. A green result from a test that cannot go red is
  indistinguishable from a green result that means something. This suite makes it
  easy to get wrong: it provisions a real throwaway Postgres, so a "dependency is
  down" fixture that patches only the seam today's code calls leaves every other
  route live and the test passes against the bug it was written to catch — patch
  **every** route (`base.db.connect` *and* `base.db.pool`), and prove it red.
  ([postmortem](../../../postmortems/0002-db-down-tests-pass-for-the-wrong-reason.md))
- **Verify the world, not the self-report.** An end-to-end assertion re-runs the
  command or re-reads the file **externally**, and asserts that untouched files
  are byte-identical. Never grep an agent's own output for success claims: an
  agent that merely *says* it did the thing passes a keyword probe, and so does
  one that did it wrong and narrated it well. The report is the thing under test,
  not the evidence.

## E2E tests (tests/e2e/)

Cross-process happy path: mock LLM (scripted fixture) + real gateway / real agent
subprocess / real Next.js dev server / real Playwright Chromium.
Uses throwaway Postgres + Redis (reuses `tests/_containers.py`), not Docker.

```bash
# Prerequisites (discard an inherited VIRTUAL_ENV — see the worktree rule above)
env -u VIRTUAL_ENV uv sync
.venv/bin/playwright install chromium

# Run (locally): one scenario file, no `-n` (e2e cannot run in parallel with itself)
.venv/bin/pytest tests/e2e/test_message_flow.py -v

# Watch the real browser
HEADED=1 .venv/bin/pytest tests/e2e/test_message_flow.py -v
```

Run only the scenario file you changed or are reproducing; the whole `tests/e2e/`
directory is a full suite and belongs to CI's independent `e2e` job
(`.github/workflows/ci.yml`), which on failure uploads
`tmp/e2e-logs/` + `~/.ava/logs/agent-*.log` as artifacts.

**Resource isolation** (runs concurrently with dev, but e2e cannot run in parallel with itself):

| Resource          | dev               | e2e               |
|-------------------|-------------------|-------------------|
| Gateway port      | 8000              | 8001              |
| Frontend port     | 3000              | 3001              |
| DB                | `ava`             | throwaway native PG (per worker) |
| Redis events ch.  | `ava:events`      | `ava:events:e2e`  |
| AVA_HOME          | `~/.ava`          | `tmp/ava_e2e_home/` |

**LLM mock injection path**: `AVA_LLM_OVERRIDE=tests.e2e.fakes.scenarios.<name>:build`
→ `base/lm/factory.py:build_chat_model` detects env and goes through importlib + factory; unset env
takes the original path (no impact in prod).

See `tests/e2e/README.md` for details.

## Leaked throwaway Postgres (`shmmni` wedge)

A throwaway postmaster is detached, so a run killed with Ctrl-C or SIGKILL — a
dev box interrupt, an agent dying mid-run — leaves it running: no `finally`, no
`atexit`, no signal handler executes in a killed process. Each survivor holds one
System V shared-memory segment, and macOS ships `kern.sysv.shmmni=32`
(`sysctl kern.sysv.shmmni`), so ~31 interrupted runs wedge the box — at that point
`initdb`/`pg_ctl start` fails for **every** cluster, including a real `ava start`.

This is self-limiting now: each throwaway instance holds an `flock` on an
`owner.lock` inside its own instance dir (`<throwaway base>/ava-pg-*/owner.lock`)
for its whole life, and the next `throwaway_postgres` reaps the instances whose
lock the kernel has released. So a killed run's orphan lives until the next test
run, not until reboot, and only instances that positively identify as throwaway
are ever touched (`base/cluster/dataplane/pg_tools.py` documents the safety argument). The lock
sits in the instance dir rather than a side registry so that it shares that
cluster's exact lifetime — nothing can prune the lock while the cluster it
describes keeps running — and so two UNIX users on one shared scratch base
(`/dev/shm`, `/var/tmp`) never contend for a shared directory. The base is the
platform default (`/dev/shm` on Linux, else the OS temp dir); a restore that
declares its footprint may land on the disk fallback (`/var/tmp`, or
`AVA_PG_THROWAWAY_BASE` when set) — the sweep covers every base
(`base/pg_throwaway_base.throwaway_roots`).

To sweep without starting a test run — e.g. a box wedged right now:

```bash
.venv/bin/python -c 'from base.cluster.dataplane.pg_tools import sweep_orphaned_throwaway_clusters as s; print(s())'
```

Instances leaked *before* this mechanism existed carry no lock, so the sweep cannot
claim them — deliberately, since identifying them would mean guessing by exclusion.
That set is **closed**: everything current code creates carries a lock, so it is a
one-time hand clear, not a missing feature.

Doing that by hand, two things save you from stopping the wrong postmaster:

- **`ppid` does not discriminate.** Every postmaster on the box has `ppid 1`, real
  clusters included — they are all detached, which is the whole reason they survive.
- **The path does.** A real cluster's data dir is `$AVA_HOME/pg` (`~/.ava`); a throwaway's is `<throwaway base>/ava-pg-*/data`. That is
  the same distinction `_resolved_throwaway_dir` encodes, and on a live box it
  separates real clusters from corpses immediately.

Inspect first — a live `ava-pg-*` postmaster may be a test run in flight in another
worktree rather than an orphan, and age is what tells them apart:

```bash
# every throwaway base: the operator override, the platform default, the disk fallback
for base in "${AVA_PG_THROWAWAY_BASE:-}" /dev/shm /var/tmp "${TMPDIR:-/tmp}"; do
  [ -d "$base" ] || continue
  for d in "$base"/ava-pg-*/data; do
    pid=$(sed -n 1p "$d/postmaster.pid" 2>/dev/null) || continue
    kill -0 "$pid" 2>/dev/null && echo "$(ps -o etime= -p "$pid") $d"
  done
done
```

Then stop only the ones older than any run you have going, one at a time:
`pg_ctl -D <data dir> -m immediate stop`. Instance dirs with **no** live postmaster
hold no shared-memory segment, so they are disk/tmpfs residue rather than part of
the wedge — they can wait for the next reboot.


## Cleaning up processes safely (pkill discipline)

2026-08-06 incident: a cleanup step ran `pkill -f 'pgbouncer.ini'` while sweeping
test residue and killed the **prod** pgbouncer — `pkill -f` matched by substring
on the config filename, and prod's data plane (port 6433) was down for minutes
until the watchdog restarted it. The throwaway-postgres sweep above is the model
that prevents this: identify processes by the instance directory (the path),
never by process or config name.

Rules for killing processes during test cleanup:

1. **Never match a bare filename substring with `pkill -f`.** `pkill -f foo`
   matches every command line containing `foo` — prod and throwaway alike. A
   cleanup may only kill processes whose command line is anchored to the temp
   directory it created (`pkill -f '<abs tmp dir>'`), or better: kill by exact PID.
2. **Inspect before you kill**: run `pgrep -fl <pattern>` first, read the full
   command lines, and confirm every hit is yours. If any hit could be prod, stop
   and pick a narrower key.
3. **Prefer a distinguishing key that cannot collide**: the process cwd (e.g.
   pgbouncer `chdir`s into its config directory, so cwd separates prod from
   throwaway), the exact data-dir path, or PIDs from a lock/pidfile you own —
   anything but a substring match on a config or binary name.
4. **When in doubt, kill by PID** from a pidfile or lock you own, not by pattern.
