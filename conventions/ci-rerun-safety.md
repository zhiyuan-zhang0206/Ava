# CI retry and concurrency safety

Automatic failed-job retries require an open PR whose current head and base/head
repository identities match the completed run. Push retries require the current
branch tip. Missing identity or API errors do not authorize a retry. Only the
first failed attempt is retried; a cap-cancelled run (`cancelled`) follows the
same single-attempt policy as a failure, never a second rerun (task #3239). The
trigger whitelist is per-workflow: besides CI, only `Inactive runtime
preparation` is retried, and only when its failure shape is the known
cold-offline family (task #3285); every other workflow or shape is left to
manual triage.

Re-runs are admissible only once the run reads `completed`: while any job is
still going, the job-level endpoint answers 403 "The workflow run containing
this job is already running" and the run-level endpoints answer 403 "This
workflow is already running" (probed 2026-09-17, task #3764; the REST docs
state the 30-day window, not this precondition). `ci-rerun.yml` therefore
fires on `workflow_run: completed`, and `--rerun-failed-jobs` reports a
still-running run as waiting with the recovery action instead of forwarding
the 403.

The guard is not atomic with GitHub's rerun API. CI therefore partitions native
concurrency by ref **and immutable tested revision (`github.sha`)**. For PRs this
also distinguishes merge revisions when the base changes without a head change.
An old attempt admitted just after
a synchronize event cannot cancel the new head's running or pending workflow.
Same-head attempts remain serialized; ordering within that group is not a proof
of freshness. Different heads may consume capacity concurrently: a new commit
does not automatically cancel its old head. Exact obsolete runs can be cancelled
by an authorized maintainer without losing completed evidence.

GitHub documents that [reruns preserve the original SHA and ref](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs),
and that [concurrency can replace pending runs even without cancelling running runs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
Changing only `cancel-in-progress` cannot close the cross-head race. These native
semantics are the boundary; no parallel scheduler, polling loop, or repository
setting is introduced.

Tests execute the actual retry shell with a mocked GitHub API and check the
workflow's cross-SHA grouping contract. They are not a hosted GitHub scheduler
simulation. Old historical workflows retain their old group definition when
rerun; new SHA-qualified groups are separate from those legacy ref-only groups.
