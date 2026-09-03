# CI retry and concurrency safety

Automatic failed-job retries require an open PR whose current head and base/head
repository identities match the completed run. Push retries require the current
branch tip. Missing identity or API errors do not authorize a retry. Only the
first failed attempt is retried, and cancelled runs are not retried.

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
