---
name: run-local-tests
description: Runs the Ava repo's Python, frontend, and end-to-end checks and diagnoses wedged local test infrastructure. Use before pushing any code change, or when pytest, initdb, Postgres, or cleanup processes will not start normally.
---

# Run local tests

## Test layering

- **Commit hook = lint only** (seconds, zero containers). pre-commit runs
  ruff, pyright, eslint, tsc, vitest (frontend), and custom lints — no pytest.
- **Local tests before push** — mandatory. After commit and before `git push`,
  run tests for the areas you touched:
  - Python: `.venv/bin/pytest <touched-test-files>` (at minimum; wider is fine)
  - Frontend: `cd ui/web && npx vitest run && npx eslint . --max-warnings 0 && npx next typegen && npx tsc --noEmit`
  Failures must be fixed before pushing; do not rely on CI to catch them.
  A new test must be **shown to fail without the fix** — run it against the
  stashed pre-change code, or invert its assertion momentarily.
- **Pick the areas by dependency, not by directory.** "Touched areas" means the
  areas a change can break, and for anything in `shared/` that is the whole
  suite: `shared/` sits at the bottom of the import layering, and the repo
  deliberately places exhaustiveness assertions over enums and field sets in the
  *consumer's* test file as review forcing functions — so a `shared/` edit is
  asserted over in `tests/gateway/`, where edit-adjacency will never look. Run
  `pytest tests -q --ignore=tests/e2e`. Cheap narrowing aid for a new enum
  member, not a substitute: `grep -rn "set(<EnumName>)\|list(<EnumName>)" tests/`.
  ([postmortem](../../../postmortems/0003-touched-areas-is-not-the-blast-radius.md))

- **Every pre-commit lint hook also runs in CI**, so a locally skipped hook is
  still caught before merge: the backend job runs `pre-commit run --all-files`,
  and a markdown-only PR (which skips backend by the path filter) gets the same
  run from the `docs-only` job. That redundancy is what makes `SKIP=` safe and
  `--no-verify` merely invisible rather than actually permissive.
- **The reverse is not symmetric, deliberately.** A few CI steps have no local
  hook because they need a toolchain a dev machine may not have:
  `scripts/migration_smoke.py` boots a throwaway Postgres and shells out to
  `psql`. A hook that fails for reasons unrelated to your commit is what breeds
  the `--no-verify` habit, so it stays CI-only. The cheap half of the migration
  gate (`scripts/lint_migrations.py` — filename format, up/down pairing,
  baseline seed) *is* a local hook, gated on `migrations/` + `db/schema.sql`.
- **Full non-e2e + e2e + coverage threshold runs in CI** — it's the merge gate.
- **Optional pre-push hook**: `ln -s ../../scripts/pre-push-check.sh .git/hooks/pre-push`
  to auto-run tests for changed areas before every push.

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
  **every** route (`shared.db.connect` *and* `shared.db.pool`), and prove it red.
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
# Prerequisites
uv sync
.venv/bin/playwright install chromium

# Run (locally)
.venv/bin/pytest tests/e2e/ -v

# Watch the real browser
HEADED=1 .venv/bin/pytest tests/e2e/ -v
```

CI runs as an independent `e2e` job (`.github/workflows/ci.yml`); on failure uploads
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
→ `shared/lm/factory.py:build_chat_model` detects env and goes through importlib + factory; unset env
takes the original path (no impact in prod).

**Three-layer env inheritance**: pytest setenv → gateway subprocess → gateway launches the
agent detached with an explicit child env dict (`ops.agent_launch.agent_spawn_env_dict`) —
nothing is inherited implicitly, and no value rides argv (issue #974).

See `tests/e2e/README.md` for details.

## Leaked throwaway Postgres (`shmmni` wedge)

A throwaway postmaster is detached, so a run killed with Ctrl-C or SIGKILL — a
dev box interrupt, an agent dying mid-run — leaves it running: no `finally`, no
`atexit`, no signal handler executes in a killed process. Each survivor holds one
System V shared-memory segment, and macOS ships `kern.sysv.shmmni=32`
(`sysctl kern.sysv.shmmni`), so ~31 interrupted runs wedge the box — at that point
`initdb`/`pg_ctl start` fails for **every** cluster, including a real `ava start`.

This is self-limiting now: each throwaway instance holds an `flock` on an
`owner.lock` inside its own instance dir (`<tmpfs base>/ava-pg-*/owner.lock`) for
its whole life, and the next `throwaway_postgres` reaps the instances whose lock
the kernel has released. So a killed run's orphan lives until the next test run,
not until reboot, and only instances that positively identify as throwaway are
ever touched (`shared/pg_tools.py` documents the safety argument). The lock sits
in the instance dir rather than a side registry so that it shares that cluster's
exact lifetime — nothing can prune the lock while the cluster it describes keeps
running — and so two UNIX users on one `/dev/shm` never contend for a shared
directory.

To sweep without starting a test run — e.g. a box wedged right now:

```bash
.venv/bin/python -c 'from shared.pg_tools import sweep_orphaned_throwaway_clusters as s; print(s())'
```

Instances leaked *before* this mechanism existed carry no lock, so the sweep cannot
claim them — deliberately, since identifying them would mean guessing by exclusion.
That set is **closed**: everything current code creates carries a lock, so it is a
one-time hand clear, not a missing feature.

Doing that by hand, two things save you from stopping the wrong postmaster:

- **`ppid` does not discriminate.** Every postmaster on the box has `ppid 1`, real
  clusters included — they are all detached, which is the whole reason they survive.
- **The path does.** A real cluster's data dir is `$AVA_HOME/pg` (`~/.ava`,
  `~/.ava-<worktree>`); a throwaway's is `<tmpfs base>/ava-pg-*/data`. That is the
  same distinction `_resolved_throwaway_dir` encodes, and on a live box it separates
  real clusters from corpses immediately.

Inspect first — a live `ava-pg-*` postmaster may be a test run in flight in another
worktree rather than an orphan, and age is what tells them apart:

```bash
base=${TMPDIR:-/tmp}; [ -d /dev/shm ] && base=/dev/shm
for d in "$base"/ava-pg-*/data; do
  pid=$(sed -n 1p "$d/postmaster.pid" 2>/dev/null) || continue
  kill -0 "$pid" 2>/dev/null && echo "$(ps -o etime= -p "$pid") $d"
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
