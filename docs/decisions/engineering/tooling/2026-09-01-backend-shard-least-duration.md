# Backend shard least-duration balancing

## Decision

The backend CI job's 12 pytest-split shards use the `least_duration` (LPT)
algorithm. The isolated backend measurements use the identical split argument,
so refreshed timings model the shard allocation that CI executes.

The default sequential `duration_based_chunks` algorithm was rejected for the
backend suite because it is sensitive to collection order. Repeated CI runs
showed materially uneven backend shards despite the committed duration data;
the equivalent e2e LPT split was balanced.

## Consequences

- Existing and newly refreshed entries in `.test_durations` are assigned with
  LPT when backend CI runs.
- The duration-refresh test locks the backend's CI-shaped pytest arguments,
  including the selection algorithm.
- CI remains the final validation of the expected lower backend-shard tail
  latency on GitHub-hosted runners.
