# Testing Ava changes

Run tests from a development checkout, separate from any running deployment.
Select affected test files and their contract consumers; a changed `base/` module
can break assertions in gateway, ops or service tests. Use
`.venv/bin/python scripts/audit/where_used.py <module-or-symbol>` to find them.
Full backend, frontend and e2e verification runs in CI.

## Environment

Use the checkout's own real `.venv`, not a symlink to another environment.
`bash scripts/setup-worktree.sh <task>` prepares an isolated checkout with locked
Python and frontend dependencies. Install shared Git hooks from a stable
checkout; see the [hook runbook](runbook.md#git-hooks-pre-commit--pre-push).

For manual dependency operations, discard inherited `VIRTUAL_ENV` and run the
editable-install guard before syncing:

```bash
env -u VIRTUAL_ENV python scripts/host_ops/guard_editable_venv.py .
env -u VIRTUAL_ENV uv sync --frozen
```

Pytest isolates its home and defaults to throwaway native Postgres/Redis. Other development
tools that import application code must set a temporary `AVA_HOME` before doing
so; unset `AVA_HOME` selects `~/.ava`, which can be a running deployment.
See [development setup](dev-setup.md#development-in-a-worktree).

## Python

Run explicit files or node IDs, including relevant consumer tests:

```bash
.venv/bin/pytest -n 2 <selected-test-files-or-node-ids>
```

For the owned audit, content lint, lint and structure tool contracts, run
`.venv/bin/pytest --test-environment=static`. This scoped process refuses native
Postgres/Redis and accepts its owned component directories, descendant paths
and node IDs; other tests keep the native default. CI runs it in the required
structure job and excludes the same paths from native shards. See [fixture environments](../../tests/fixtures/docs/static-environment.ava.okf.md).

Check changed Python files with pyright:

```bash
git diff --name-only --diff-filter=ACMR -z origin/main...HEAD -- '*.py' \
  | xargs -0 -r .venv/bin/pyright
```

Pytest paths are grouped by directory by the collection guard: collecting the
same directory twice can hide its conftest fixtures. Python children started
with `-I` ignore `PYTHONPATH`; cross-process tests need this checkout's own
editable environment. For native lifecycle behavior, use the release interpreter
build and exercise cleanup as well as startup.

## Frontend

From `ui/web`, run the affected tests and eslint, then check generated Next.js
routes and types after the last UI edit:

```bash
npx vitest related --run <changed-source-files>
npx eslint <changed-files>
npx next typegen
npx tsc --noEmit
```

Use `npx vitest run <selected-test-files>` when selecting test files directly.
Regenerate OpenAPI and frontend types with `bash scripts/codegen/codegen-types.sh` from
the repository root; do not hand-edit generated output.

## End-to-end

The e2e harness uses a scripted LLM, real gateway/agent processes, native throwaway
Postgres/Redis, Next.js and Playwright Chromium. Run one relevant scenario file
without xdist: these scenarios share ports and cannot run in parallel locally.

```bash
.venv/bin/playwright install chromium
.venv/bin/pytest tests/e2e/flow/test_message_flow.py -v
HEADED=1 .venv/bin/pytest tests/e2e/flow/test_message_flow.py -v
```

See [the e2e harness](../../tests/e2e/README.md) for configuration. Match verification
to observable effects: re-read actual state rather than trusting an agent's
success report. A regression test should fail when its target defect is restored;
a deliberately inverted assertion alone does not demonstrate that property.

## Checks and evidence

Commit hooks check changed files; pre-push hooks also check the branch diff,
imports, types and generated-artifact freshness. CI independently runs the
corresponding checks, including migration smoke and full test coverage. The
frontend pre-push selector checks the branch contribution, using changed
tests, related source paths and known filesystem consumers. It reports global
and deletion closure as CI-only; a passing subset does not certify omitted
consumers. Full suites remain CI-only. Fix genuine hook failures and report any checks that could not run. Do not use `--no-verify` to
bypass unrelated checks.

The generated HTTP OpenAPI schema and TypeScript declarations have no runtime
Vitest imports. Their changes retain project typechecking, codegen freshness and
known filesystem consumers; runtime source edits still use related-test selection.

Record the tested revision and relevant commands/results. A missing workflow,
empty test collection or skipped affected check is not successful verification.

## Throwaway Postgres and cleanup

Interrupted test processes can leave detached postmasters. Each current throwaway
instance holds `owner.lock`; the next test run reaps instances whose locks were
released. To invoke the supported sweep directly:

```bash
.venv/bin/python -c 'from base.cluster.dataplane.pg_tools import sweep_orphaned_throwaway_clusters as s; print(s())'
```

The sweep only claims positively identified throwaway instances. Do not remove a
live lock or identify an orphan merely by age, process name or parent PID; real
clusters also detach. For manual investigation, identify the exact temporary
data directory and its owner before acting. Never use a bare filename substring
with `pkill -f`: it can match production too. Prefer exact owned PIDs and inspect
the full command line and native process identity before cleanup. Preserve a
checkout while live processes or sessions are anchored there.
