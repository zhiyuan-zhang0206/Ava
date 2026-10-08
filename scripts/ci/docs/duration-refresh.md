# Duration refresh cadence

Every CI collection already recalculates least-duration shard assignments.
Refreshing updates the shared input weights, rather than saving fixed shard
membership. Unknown nodes use pytest-split's average estimate.

`duration_refresh_policy.py` owns the 20-change interval. It counts first-parent
main commits since the last **published complete measurement's source SHA**.
A squash or merge PR counts once; direct commits count too. Upon a successful
main-push `CI` completion at or beyond that interval, the refresh workflow reuses
that run's 16 backend and four e2e artifacts. A CI run with skipped suites does
not provide a complete snapshot; the refresh refuses it and the next complete
main CI or nightly measurement supplies the data. Recording adds flags to the
existing pytest execution and creates no extra test runs. Retry attempts reseed
from committed weights before collection; partial measurements never affect
selection. Upload failure remains informational in CI and causes the refresh's
complete-artifact check to fail rather than publish partial data.

Daily isolated measurements remain the fallback at 19:30 UTC, with a 21:30
backstop. GitHub may delay cron execution. A complete measurement published in
the preceding five hours suppresses another scheduled measurement; successful
no-op runs never reset freshness. Manual dispatch always measures. A legacy
cache without provenance bootstraps at the next successful main CI or schedule.
Only the publisher has write permissions; planning and measurement use read
permissions. Cancellation prevents publication.

All fallback shards, artifact merging, and the candidate branch use one source
SHA selected by the plan. Reused artifacts come from exactly one successful
main-push CI run in the same repository. PR, fork, failed, unrelated-workflow,
and sources outside main history are refused before publication. Artifacts
download into runner temporary storage, never over executable repository files.
Missing, empty or overlapping shards fail before the committed cache changes.
The native backend population excludes the separately owned static tests, just
as normal CI does; coverage and worker counts match CI.

After merging complete timings, the workflow creates `.test_durations.source.json`
with schema version, measured source SHA, Actions run ID and measurement time.
Both files publish in one Git commit on `ava-bot/test-durations`. The branch is
rebuilt from the measured source, so its required CI checks see that generation's
code. One existing bot PR is updated and explicitly receives `ci.yml` dispatch.
This PR is never auto-merged.

The plan reports applied provenance from main separately from the open bot PR's
published provenance. Pending publication resets the refresh counter to avoid
rewriting the PR on every main push; it does **not** mean main uses those weights.
Review and merge the bot PR to apply them. Later node-ID maintenance can edit
the timing map without claiming a new measurement by changing provenance.
Closed unmerged bot PRs do not reset the counter. Older main CI completions
cannot overwrite a newer published snapshot. API errors or malformed provenance
fail visibly rather than guessing freshness. Required CI gates stay unchanged.
