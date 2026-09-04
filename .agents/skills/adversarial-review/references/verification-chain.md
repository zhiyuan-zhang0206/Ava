# Verification chain — proving the green is real

The 2026-08-05/06 fake-green series (36 h) established one rule: **CI green
is a claim, not a fact.** GitHub status showed "operational" while runners
executed nothing. Every review starts by validating the PR's verification,
because reviewing an unverified change is theater. Do this before reading
the diff.

## 1. Read the run, not the check

`gh pr checks <n>` only shows pass/fail. Dig into the runs:

```bash
gh pr checks <n>                                   # which checks exist
gh run list --branch <branch> --limit 5 --json databaseId,workflowName,status,conclusion
gh run view <run-id> --json jobs,createdAt,updatedAt
gh run view <run-id> --log-failed
```

For every job that "passed", verify:

| Check | Honest signature | Fake-green signature |
|---|---|---|
| backend-parallel | >= 4 min wall time; pytest junitxml artifact; coverage gate parsed a real number | finishes in seconds; no artifacts; coverage gate skipped |
| backend-serial | flaky group actually runs (nonempty) | `-m flaky` with nothing selected, still green |
| frontend | vitest/eslint/tsc all ran; nonempty test counts | flaky group with `\|\| true` swallowed a failure |
| e2e | Playwright produced results; artifacts exist | no junitxml, no artifact assertion |
| any job | the log shows commands executing | log empty or truncated; "0 tests collected" |

## 2. The proof-of-work checklist

- **Durations**: a full backend suite in under ~2 minutes is the documented
  fake-green signature (0-byte `uv` in the runner snapshot). Trust nothing
  that fast; rerun or run locally.
- **Artifacts**: `upload-artifact` with `if-no-files-found: error` is the
  tripwire that makes a no-op job fail. If the PR touches a stack whose job
  lacks a tripwire, that is a finding (G5).
- **Coverage**: the gate must have parsed a number from a real
  `coverage.xml`. A gate whose env fallback is lower than the claimed
  threshold is a finding (B4).
- **`NO_WORKFLOW_RUNS`** is not green — the suite never ran. A skipped
  check must have a skip reason that matches the diff scope, even when branch
  protection accepts the skipped result.

## 3. Local verification

When CI is suspect, or the diff touches something CI does not cover (the
middle integration layer is a documented vacuum — 9 integration tests for
6000+ units), run the narrow check yourself and cite it in the report:

```bash
# in the worktree
bash scripts/setup-worktree.sh   # first use only
.venv/bin/pytest <touched-test-files> -q
.venv/bin/pyright                  # pre-commit will gate anyway
```

See `.agents/skills/run-local-tests/SKILL.md` for the full matrix. Rules:

- A failing local run is always a finding; a passing local run is only
  evidence when it exercised the changed path.
- Never run dev/test scripts that can touch production (os_cron
  registration, migrations, IM sends) outside the pytest guardrails — a
  non-pytest script does not get conftest's env lockdown (G6).

## 4. What "green" means for the gate

The gate before enqueue is: **CI genuinely green (verified per above) AND
adversarial review passed (P0/P1 resolved or user-waived).** If either is
unverifiable, the verdict is "unverified pass" — state it in the report;
it blocks enqueue just like a red run.
