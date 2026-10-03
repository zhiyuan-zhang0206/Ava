---
type: doc
title: "CI test gate and executed-test counts"
description: "How the backend jobs' Trunk quarantine gate fails closed (empty secret, missing or empty JUnit report), the per-shard and total executed-test counts CI prints, and where the root leak guard's findings are read."
tags:
  - ci
  - testing
---

# CI test gate and executed-test counts

## The gate

`backend-shard`, `backend-selected` and `backend-serial` run pytest with
`continue-on-error`, so the Trunk quarantine gate (`trunk-io/analytics-uploader`)
is what turns a pytest failure red: quarantined flaky failures pass, real ones
block. That gate used to fail open twice. It was skipped when
`TRUNK_ORG_URL_SLUG` was empty, so nothing judged pytest at all; and the
uploader's `allow-missing-junit-files` defaults to true, so a pytest that died
before writing a JUnit report (a `pytest_plugins` module that fails to import
ends the run in the configuration phase) uploaded nothing and passed with not
one test run. A pytest that ran zero tests (a broken `testpaths`) leaves an
empty report and passed the same way.

It now fails closed:

- The uploader runs with `allow-missing-junit-files: false`.
- When the secret is empty, the `require-test-gate` composite action runs
  instead: on this repository's own runs it errors (`TRUNK_ORG_URL_SLUG is
  empty, the test gate cannot run`); on a run that never gets secrets — a
  fork's pull request, or any Dependabot-triggered run (even on this
  repository's own branches) — pytest's own outcome decides.
- Each backend shard's `Report executed test counts` step is a gate: no JUnit
  report, or no executed test, reds the shard (`--min-tests 1`). The flaky
  bucket may be empty, so it only needs a report (`--min-tests 0`).

`tests/ci/test_backend_test_gate.py` pins the wiring and reproduces both
failures with a real pytest. The `e2e` and `frontend` jobs have their own
uploader steps, which this change does not touch.

## Executed-test counts

The same step counts the tests from the JUnit report pytest already wrote
(`scripts/ci/shard_counts.py`), per `tests/` directory, into the job log and
summary. The informational `backend test counts (all shards)` job adds the 16
shards and the flaky bucket up and prints the difference against the previous
main run's total, which each push to main records as the `test-count-baseline`
artifact, so a moved test that fell out of the suite shows as a changed count.
It is not required and cannot fail the run.

### Leak-guard findings

The root leak guard ([[../tests/docs/test-leak-guard.ava.okf.md]]) writes each finding as a JUnit property of the test that leaked. The same shard step carries them into `shard-counts-N.json` (`leaks`, `notes`, `faults`; absent when there are none), and the counts job reports the whole run from that one job, so a leak is read without pulling any shard log: the job summary, one `leak guard` annotation of at most 40 lines (one line per file, kind and thing leaked), and the `test-leak-report` artifact with the full list.

```bash
gh api repos/OWNER/REPO/check-runs/JOB_ID/annotations --jq '.[]|select(.title|startswith("leak guard"))|.message'
gh run download RUN_ID --repo OWNER/REPO --name test-leak-report
```

The check-run API does not expose the Actions job summary, which is why the digest is an annotation. Nothing here can fail a run: a fault of the guard is a `GUARD FAULT` line (and a property), a fault of the report one `leak guard (report fault)` annotation, and the step and the job are `continue-on-error`.

## Key dependencies

- [[.github.ava.okf.md]] — parent overview and the CI job definitions.
- [[test-selection.ava.okf.md]] — the enforced subset that `backend-selected` gates.
- [[../scripts/docs/scripts.ava.okf.md]] — the counting script.
