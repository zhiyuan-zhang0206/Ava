# CI duration refresh isolation

## Decision

The nightly `.test_durations` refresh measures the backend's 12 shards and
e2e's four shards on separate CI runners, matching the suite isolation used by
the normal gate. Each shard retries independently and uploads one clean
duration artifact. The merge requires every expected artifact and atomically
publishes the combined timings only when the set is complete.

The prior single-runner full-suite measurement was rejected for the nightly
path. Its timing and environment-sensitive failures aborted the entire refresh
despite the affected tests passing in CI's isolated shard environment. Retrying
that full run could also exceed its original job budget without restoring the
isolation on which normal CI relies.

## Consequences

- A transient measurement failure reruns only its isolated shard.
- An exhaustively failing or missing shard blocks the refresh PR rather than
  publishing a partial timing model.
- The existing review-only branch, PR title, and no-auto-merge policy remain
  unchanged.
- The first isolated refresh removes pre-existing flaky-test entries: backend
  measurements exclude them with `-m "not flaky"`, and `--clean-durations`
  retains only tests that ran in a measurement shard. Later nightly refreshes
  retain that behavior, so flaky durations do not accumulate.
- The first isolated refresh also rewrites the whole existing
  `.test_durations` file into its compact canonical JSON format. That is an
  intentional one-time format normalization; subsequent refreshes keep the
  same format.
