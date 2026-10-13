# Test durations: where they come from and how CI gets them

Every CI collection recalculates least-duration shard assignments from the per-test
timings in `.test_durations`, so the timings only balance the shards; they never
change which tests run or whether they pass. Unknown nodes cost pytest-split's
average. The file is **not in the repository** (it is gitignored): it is built
daily and distributed through the GitHub Actions cache.

## Producing it

`.github/workflows/refresh-test-durations.yml` runs daily (19:30 UTC; GitHub may
delay cron by hours) and on `workflow_dispatch`, on main only:

1. `plan` resolves the newest existing cache so the measured shards split with the
   current model (the first run has none and splits by test count).
2. `measure-backend` (16 shards) and `measure-e2e` (4) each run
   `refresh_test_durations.py measure` on their own runner: the CI pytest arguments,
   the CI coverage module list, `--store-durations --clean-durations`, three attempts
   that each reseed the temporary duration input, so a failed attempt cannot leak a
   partial measurement. The native backend population excludes the separately owned
   static tests, as normal CI does.
3. `publish` downloads the artifacts and runs `merge`, which requires all 20, rejects
   empty or overlapping shards before touching anything, keeps every measured entry
   (fast and zero timings included), rounds to three decimals and writes
   `.test_durations` atomically. The file is saved as the cache
   `test-durations-<UTC date>-<run id>-<attempt>`. The key is unique per run, so a
   same-day dispatch never collides with an earlier save (cache entries are immutable).
4. `verify` waits (bounded) until the cache listing names that key, then uses the
   same two actions as CI to resolve and restore it and checks the entry count.

A failed measurement saves nothing, so CI keeps using the previous entry. Daily
writes also keep the entry from going unused for the seven days after which Actions
evicts caches.

## Consuming it

- `classify change` runs `actions/resolve-test-durations` once per run: it lists
  `test-durations-*` caches on `refs/heads/main` (the default branch's caches are the
  ones every pull request can read), takes the newest, retries the API three times,
  then restores it into `.test_durations` for the selector. The key is a job output.
- `backend shard` and `e2e shard` run `actions/restore-test-durations` with that exact
  key. Resolving once per run matters: shards that split with different timings than
  their siblings would drop or duplicate tests, so a key that no longer restores
  (evicted mid-run) fails the shard instead of degrading silently.
- With no cache (a fresh repository, an unreadable listing) the run goes on and
  prints a `test durations` warning annotation: shards split by test count, and the
  test selector, whose 80% guard compares two sums of timings and has nothing to
  compare, returns FULL (`no-durations`).
- Development machines have no file unless `refresh_test_durations.py` (the
  single-runner `refresh` command) built one; pytest-split then warns and splits
  evenly.

The workflow needs only `actions: read` and the default token, no secret, no
repository setting and no approval step.
