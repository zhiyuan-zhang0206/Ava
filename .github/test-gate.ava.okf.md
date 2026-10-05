---
type: doc
title: "CI test gate and executed-test counts"
description: "Native test verdicts and supplier-independent JUnit validation, the per-shard and total executed-test counts CI prints, and where the root leak guard's findings are read."
tags:
  - ci
  - testing
---

# CI test gate and executed-test counts

## The gate

Backend shards, enforced selected subsets, the serial timing-sensitive bucket,
frontend Vitest and every e2e family propagate their native test command's final
exit status. No external quarantine service or repository secrets change that
verdict. The existing selector shadow job remains informational while the full
backend fan-out gates integration.

The `require-test-gate` composite action also validates each required JUnit
report, including after a native failure: missing or malformed reports, a wrong
root, no executed tests, or recorded failures/errors cannot read green. It prints
the failing test identities. Backend retries validate the final attempt; earlier
reports and the attempt-1 failure log remain available as evidence. Only the
explicitly optional backend/frontend serial buckets permit empty reports. A
backend serial pytest exit 5 is admitted only for that empty bucket; valid,
failure-free JUnit evidence is still required.

`tests/ci/test_backend_test_gate.py` exercises native failures, false-success
reports, missing or malformed evidence, optional empty buckets and retry reports,
and reproduces a crashed pytest and a zero-execution pytest. Forks, Dependabot
and canonical runs use the same test verdict.

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
