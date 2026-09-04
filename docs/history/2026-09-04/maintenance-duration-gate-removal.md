# Maintenance-duration gate removal

## Decision

The user's explicit 2026-09-04 ruling to delete the fixed-duration gate
supersedes the 2026-09-01 16:50 ruling that authorized the 120-second
admission threshold. The earlier decision remains in history as the record of
what was believed then; this decision replaces only its duration-based
admission rule.

Cluster rollout stage durations are observations, not an admission-control
boundary. `ava cluster update` no longer refuses to enter Phase A because the
predicted maintenance duration crosses a fixed threshold. Its dry-run likewise
does not derive PASS or FAIL from that prediction. Both paths still run the
prepare checks, and candidate failures such as runner reachability, target
environment construction, or daemon imports remain blocking.

The recent-stage baseline remains as rollout telemetry. Dry-run reports its p95
estimate and stage breakdown as explicitly informational context, and completed
rollouts continue to refresh the samples. No sample value can permit or refuse
a rollout.

## Evidence and self-lock mechanics

The baseline keeps at most ten samples per stage and computes nearest-rank p95
as item `ceil(0.95 * n)` in sorted order. For every retained population from one
through ten, that item is the maximum. The estimate then adds the independent
maximum of each stage, even when those maxima came from different rollouts, so
it can describe a synthetic duration no rollout actually had.

Only a CLEAN rollout appends a replacement sample. Once the sum of retained
maxima crosses the threshold, the gate rejects the rollouts that could have
produced newer samples. The maximum can therefore never age out through the
normal path: the measurement and admission loops deadlock each other.

The live history demonstrated the loop repeatedly:

- On 2026-09-02 the estimate was 126.2 seconds: stop-the-world 1.5, local leg
  40.1, readiness 0.2, and Phase B 84.4. At 23:27 the user approved a manual
  reset to `n=1` and a 110.1-second estimate; the 23:27:54 rollout then
  completed with `rc=0`.
- On 2026-09-03 the `n=4` per-stage maxima were 1.8, 88.1, 0.2, and 80.7
  seconds, synthesizing a 170.8-second estimate. The baseline was copied to
  `update-baseline.json.bak-20260903` at 12:22:21 and removed; four seconds
  later the missing-baseline 110-second seed passed. This was the second
  manual reset, not an automatic recovery.
- Excluding Phase B narrowed the estimate but did not remove the defect. On
  2026-09-04 a one-sample baseline held stop-the-world 2.0, local leg 119.8,
  readiness 0.3, and Phase B 77.4 seconds. The admitted-stage sum was 122.1
  seconds, and real updates were refused at 18:49 and 18:51. For the explicitly
  authorized temporary bypass, the file was moved recoverably to
  `$AVA_HOME/update-baseline.json.bak-20260904-215650` before this permanent
  code change was prepared.

## Rationale

Elapsed time is an outcome of the host, dependency, and target state; it does
not prove that entering maintenance is safe or unsafe. The fixed threshold also
formed a closed loop: a slow successful rollout could block every later rollout,
while only another successful rollout could refresh the samples. That converts
one observation into a persistent operational lock without a recovery path.

Adding an override was rejected because it would preserve an invalid safety
boundary and create two rollout paths. Deterministic pre-maintenance evidence
continues to carry admission control; duration telemetry remains available for
performance diagnosis and design work.
