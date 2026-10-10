# CI retry and concurrency safety

Automatic failed-job retries require an open PR whose current head and base/head
repository identities match the completed run. Push retries require the current
branch tip. Missing identity or API errors do not authorize a retry. Only the
first failed attempt is retried; a cap-cancelled run (`cancelled`) follows the
same single-attempt policy as a failure, never a second rerun (task #3239). The
trigger whitelist is per-workflow: only CI is retried; every other workflow is
left to manual triage.

Re-runs are admissible only once the run reads `completed`: while any job is
still going, the job-level endpoint answers 403 "The workflow run containing
this job is already running" and the run-level endpoints answer 403 "This
workflow is already running" (probed 2026-09-17, task #3764; the [Actions
re-run how-to](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
states the 30-day window, and no docs page states this precondition).
`ci-rerun.yml` therefore
fires on `workflow_run: completed`, and `--rerun-failed-jobs` reports a
still-running run as waiting with the recovery action instead of forwarding
the 403.

The guard is not atomic with GitHub's rerun API, and CI cancels superseded PR
runs. `ci.yml` therefore groups native concurrency as follows:

- `pull_request` runs group by ref only (`ci-refs/pull/N/merge`) with
  `cancel-in-progress`, so a new push cancels the run of the head it replaces
  instead of letting it finish and consume runner capacity.
- Every other event (`push` to main, `workflow_dispatch`) keeps the SHA in the
  group and never cancels. A group holds one running and one pending run, and a
  newer pending run replaces the older one, so a ref-only group on main would
  skip intermediate commits whose CI `refresh-test-durations.yml` consumes.
- Trunk merge-queue branches (`trunk-merge/...`) also run as `pull_request`,
  but each has its own ref, so they never share a group with a PR.

CI fan-out and summary jobs use `!cancelled()` to admit failed or skipped needs
without resisting an issued workflow cancellation. Required summaries still
reject non-success dependencies when the workflow has not been cancelled;
step-level cleanup and diagnostic uploads retain their `always()` conditions.
GitHub documents this distinction in its
[status functions](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions)
and [cancellation sequence](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation).

The remaining race: `ci-rerun.yml` confirms the run's head is still current,
then calls the rerun API. If a push lands between those two steps, the rerun of
the old head enters the PR's group (reruns keep the original ref and
`GITHUB_REF`, per the [Actions re-run how-to](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs))
and cancels the new head's running run. This heals itself through the existing
retry path, with no extra mechanism:

1. The cancelled new-head run completes as `cancelled`, which `ci-rerun.yml`
   treats like a failure (attempt 1, `pull_request` or `push` event).
2. The head check now passes for it (it is the current head), and it is the
   newest CI run for that SHA, so the guards admit it.
3. `rerun-failed-jobs` re-runs its cancelled and failed jobs and their
   dependents. The REST reference describes the endpoint only as re-running
   "all of the failed jobs and their dependent jobs"
   ([docs](https://docs.github.com/en/rest/actions/workflow-runs#re-run-failed-jobs-from-a-workflow-run));
   that cancelled jobs count is not stated there but is the premise of the
   cap-cancel policy above and has been observed on this repository's CI runs.
4. The new attempt enters the same group, where `cancel-in-progress` cancels the
   still-running rerun of the old head. That old attempt has `run_attempt > 1`,
   so `ci-rerun.yml` does not retry it.

The cost is one extra CI attempt in a rare window, and the PR shows a
cancelled check until the retry finishes. Each superseded run also ends
`cancelled` and starts one short `ci-rerun.yml` job, whose head check then
refuses the retry. No parallel scheduler, polling loop, or repository setting
is introduced; the native semantics are the boundary, and the
[concurrency docs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
define group replacement and cancellation.

Tests execute the actual retry shell with a mocked GitHub API and evaluate
`ci.yml`'s concurrency group per event. They are not a hosted GitHub scheduler
simulation. Old historical workflows retain their old group definition when
rerun.
