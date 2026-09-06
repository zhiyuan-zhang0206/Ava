# Flaky-test checklist

Read this before writing tests that touch processes, timing, ports, environment,
or durable state, and before triaging a CI failure. It records every CI flake
class that has occurred here and the rule that prevents it.

| Anti-pattern | Lint verdict | Guard |
|---|---|---|
| Assert immediately after kill | lintable with new rule | New: AST heuristic for process-table asserts |
| Treat one red attempt as a regression | not lintable (operational) | Existing: attempt-history triage and rerun cap |
| Read or mutate ambient host state | lintable now | Existing: fixture-scope and environment-write lints |
| Depend on ordering | lintable with new rule | New: fixed-port heuristic; isolation guards |
| Quarantine timing races by default | not lintable (operational) | Existing: serial-group admission and exit policy |
| Assume runner-load readiness | lintable with new rule | New: fake-timer-loop heuristic; bounded waits |
| Pin wall-clock values or counts | lintable now | Existing: clock-lattice lint and topology tests |
| Generate visual references outside the comparison runner | not lintable (operational) | Runner-native check and refresh workflow |

## 1. Do not assert process-table disappearance immediately after a kill

**Rule.** A killed process tree is gone when it has no live members. A zombie is
not live, even while the process table still contains its PID.

**Signature.** An assertion such as `assert not psutil.pid_exists(pid)` follows
`kill`, `close`, or cancellation immediately; `is_running()` reports `True` for
a dead process; CI reports `assert not True` and a rerun is green.

**Evidence.** PR #306 changed three such assertions in
`tests/agent/test_exec_subprocess.py`, including
`test_natural_exit_reaps_ordinary_descendant_holding_stdout`.
`ExecProcessDomain.close()` promises `SIGKILL` delivery, not immediate
process-table removal. A descendant can remain a zombie until an OS reaper
asynchronously collects it, and `pid_exists` remains true meanwhile. PR #964
applied the same discipline to the forced-shutdown PITR test in
`tests/services/test_pitr_base_scheduler.py`; its root-cause fix was a daemon
ownership-adoption `Event`, not a test relaxation. PR #1303 made
`shared/posixproc.py` liveness zombie-aware
(`is_running()` and `status() != STATUS_ZOMBIE`) and exposed a separate
regression: `_FakeProc` did not implement the new `status()` probe, so CI shard
8 failed twice at the same `AttributeError`.

**Correct form.** Poll with a deadline for no *live* members; treat
`NoSuchProcess` and `STATUS_ZOMBIE` as gone. Keep the production kill contract
unchanged unless production semantics are independently wrong.

```python
def _assert_tree_gone(pids: set[int], timeout_s: float = 5) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(_pid_is_gone_or_zombie(pid) for pid in pids):
            return
        time.sleep(0.05)
    assert all(_pid_is_gone_or_zombie(pid) for pid in pids)
```

If production code begins reading another `psutil.Process` attribute, update
every test double that models that probe contract.

**Lintability — lintable with new rule.** An AST heuristic can flag
`pid_exists`-style assertions in tests, following the AST approach used by the
[fixture-scope lint](../scripts/lint_fixture_scope.py); zombie semantics still
need human review.

## 2. The same point twice is a regression

**Rule.** Read attempt history before blaming a diff. One failed attempt
followed by a green retry is a flake; two consecutive attempts failing at the
same point are a regression until disproved.

**Signature.** A merge-queue or CI run is red and someone proposes another
rerun without comparing its failure location with the preceding attempt.

**Evidence.** In PR #928, a speculative merge-queue attempt failed once and the
next attempt was green, so it auto-requeued without human action. In PR #1303,
CI shard 8 failed twice at the `_FakeProc` `status()` `AttributeError`; that
repeat correctly identified a real probe-contract break.

**Correct form.** Check attempt history first and rerun once to classify the
failure. If the same test and failure point fail twice consecutively,
investigate it as a regression. Do not launch a third automatic rerun without a
filed case; appendix A defines the release gate.

**Lintability — not lintable (operational).** Attempt history is CI state, so
the rule belongs in QA or merge-queue tooling rather than a repository lint.

## 3. Redirect resources; never inherit ambient host state

**Rule.** Test isolation comes from redirecting the value a process reads, then
patching any durable-state read that must not inherit a worker's shared home.

**Signature.** A result depends on process environment left by an earlier test,
a marker under worker-shared `$AVA_HOME`, a real host service such as launchd,
cron, or Telegram, or the operator's live cluster.

**Evidence.** `tests/e2e/conftest.py:_e2e_process_env` was session-scoped, so
its environment keys leaked to later tests in the same process. That made
`tests/test_home_isolation.py` fail in serial runs while parallel CI hid the
leak. The fix narrowed scope and added
`test_the_e2e_fixture_restores_every_env_key_it_assigns`. On 2026-08-31,
`tests/cli` calls using `--disable-service restarter` durably wrote
`$AVA_HOME/disabled_services`; later `TestUnpauseLocalCluster` tests silently
early-returned, producing `spawned == []` or `DID NOT RAISE` in PRs #1172 and
#1173. PR #1185 redirected each writer's marker and PR #1204 made the victim
read hermetic with a tripwire. PR #1224 fixed another class that read the real
durable marker. In the 2026-08-07 P0 (pre-cutover), a non-pytest
debug script inherited the real environment and rewrote the production launchd
health-probe plist, restarting the cluster.

**Correct form.** Let `tests/conftest.py` redirect `$AVA_HOME`, database,
Redis, registry, ports, and session home. Patch durable-marker reads in the
fixture owning the test, rather than inheriting worker state. Do not use a
non-root, session-scoped fixture to mutate process globals. The OS scheduler
cannot be redirected: keep `AVA_OS_JOBS_ENABLED=false`, and never run a
non-pytest script that imports `gateway.app` from a worktree.

**Lintability — lintable now.** The
[fixture-scope lint](../scripts/lint_fixture_scope.py) rejects session-scoped
process-global mutation outside root `tests/conftest.py`, and the existing
`lint_no_os_environ.py` catches source-side environment writes. Hermetic
durable-marker reads remain a review heuristic and new-rule candidate; see
[the testing guide](../tests/README.md#host-isolation-what-a-test-run-may-touch).

## 4. Do not depend on collection order or shared worker state

**Rule.** Every test must own its ports and durable state. A test that passes
alone but fails in the suite has an ordering dependency until proved otherwise.

**Signature.** Fixed ports collide across xdist workers; directory or file
order changes a result; durable state written by one test affects another; a
failure disappears when the test runs alone.

**Evidence.** PR #387(b) found `test_ui` binding `10000 + agent_id`; IDs began
at 1 in every worker database, so workers collided. Socket tests now bind
`port=0` and read back the assigned port; timeout tests use a silent listener so
another worker's stub cannot answer `/health`. The e2e environment leak above
also depended on `tests/e2e/` collecting before `tests/test_home_isolation.py`,
so their guard now runs in one serial worker. The 2026-08-31 marker poisoning
in PRs #1172 and #1173 was another order dependency.

**Correct form.** Bind `port=0`, retrieve the assigned port, and keep durable
state per test. Do not rely on collection order. When an ordering is genuinely
load-bearing, create an explicit serial grouping and a guard test in a
merge-gated job.

**Lintability — lintable with new rule.** A grep or AST rule can flag fixed-port
binds in tests, but collection order and cross-test durable-state dependence are
not statically decidable.

## 5. Quarantine only timing races whose root cause is still unknown

**Rule.** Find a deterministic cause before serializing a test. The `flaky`
marker is a temporary quarantine, not a cure for a race against a daemon or the
OS.

**Signature.** A timing race recurs under runner load and the proposed fix is
to add `@pytest.mark.flaky` without a root-cause analysis.

**Evidence.** The serial bucket is real infrastructure: `@pytest.mark.flaky`
runs in the backend serial job (`-m flaky -n 1`), and the frontend has
`vitest.flaky.config.ts`. The split predates the 2026-08-18 public-repo
cutover, so its original PR number is not reachable from public main; the
internal-CI migration rewrite (commit `8f00cc49b`) silently dropped it, and
PR #433 restored it with the frontend coverage gate. PRs #306 and #964 show why serial execution is not a
fix for every timing race: their opponents were the asynchronous OS reaper and
daemon readiness, not sibling tests.

**Correct form.** First make the test deterministic with an ownership-adoption
gate, bounded poll, or hermetic read. Admit a test only when its root cause is
unknown and it has failed at least twice in one day; appendix C governs exit.

**Lintability — not lintable (operational).** Marker admission and removal are
QA decisions. CI can audit marker counts and justification, but no repository
lint can determine whether the root cause remains unknown.

## 6. Wait for the production read path, not an assumed runner speed

**Rule.** A newly spawned process and fake-timer-driven UI are not observable
on the same schedule as `Popen` or one local timer cycle.

**Signature.** Immediately after `Popen`, a test reads `/proc` argv or PID-table
state and gets `UNREADABLE` or `None` only on a busy runner. In frontend tests,
a fixed number of `runOnlyPendingTimersAsync` cycles precedes `getByRole`, and
CI randomly reports that no accessible element was found.

**Evidence.** In the `tests/ops/test_agent_identity.py` family, `stranger_pid`
spawned `sleep 60` and yielded immediately. The child's argv was sometimes not
readable: `test_foreign_for_a_real_live_stranger` saw `UNREADABLE` and
`test_reads_a_live_processes_argv` saw `cmdline is None`, documented on
2026-08-04 and 2026-08-09 and recurring in shard 7 on 2026-08-30. PR #1224
added a bounded 5-second, 20-millisecond readiness wait using the production
cmdline reader. PR #1303 has the related post-kill reparenting and zombie case.
On 2026-09-01, frontend tests with fixed fake-timer cycles passed locally but
sporadically failed under Vitest parallel load with an accessible-role error.

**Correct form.** Wait, with a deadline, for the production read path to become
ready before yielding a spawned PID; never probe in the execution window. Use
condition-driven, capped timer draining, or framework polling such as
`findByRole`. If a capped loop never reaches readiness, throw a human-readable
error.

```ts
async function drainTimersUntil(ready: () => boolean, maxCycles = 60) {
  for (let cycle = 0; cycle < maxCycles; cycle += 1) {
    if (ready()) return;
    await act(async () => {
      await vi.runOnlyPendingTimersAsync();
      await Promise.resolve();
    });
  }
  if (ready()) return;
  throw new Error("the expected element never rendered");
}
```

**Lintability — lintable with new rule.** An AST rule can identify fixed-count
fake-timer loops; process argv readiness depends on runtime scheduling and is
not statically decidable.

## 7. Assert clock relations, not a wall-clock moment

**Rule.** When timing values are load-bearing neighbours, derive assertions
from the clock lattice rather than a fixed instant, duration, or machine-speed
count.

**Signature.** A test pins a wall-clock moment or count while the code under
test uses real time or a lattice clock; a new timing constant defines a relation
outside the lattice.

**Evidence.** [`shared/timing.py`](../shared/timing.py) makes `CLOCKS` the
single authority for ordered timing constants, including boot stall, launch
confirmation, boot budget, and reap grace, as well as `NO_PROGRESS` and
`LOCK_TTL`. In the 2026-07-30 spawn incident, launch confirmation was extended
without extending its neighbouring reap grace; the required relation existed
only in prose. The 2026-08-31 serial-bucket audit of the last 500 runs found
that remaining noise was pinned-count sync gates or deterministic in-PR
regressions, not flakes.

**Correct form.** Assert a relation between registered clocks or reference the
registry. When a count must be pinned, derive it from the registry or freeze
time deterministically with fake timers; never assume a machine will reach a
state by a fixed wall-clock instant.

**Lintability — lintable now.** The
[clock-lattice lint](../scripts/lint_clock_lattice.py) rejects
lattice-vocabulary constants outside approved lattice modules, while
`tests/shared/test_timing_topology.py` verifies the declared relations.
Pinned-count gates remain a review heuristic.

## 8. Generate and compare visual references in one environment

**Rule.** A visual reference must be generated by the same GitHub-hosted runner
environment that performs the pixel comparison. A local host or a pinned
container is not an equivalent baseline source.

**Signature.** `test_home_visual_regression`, `test_fleet_visual_regression`, or
`test_mobile_visual_regression` exceeds the 0.1% changed-pixel cap by a small
margin, then passes unchanged on a later GitHub-hosted runner.

**Evidence.** Twenty-four failures occurred from 2026-09-03 through 2026-09-06.
Eight captures of each of the three pages in the CI-aligned
`mcr.microsoft.com/playwright/python:v1.59.0-noble` container were
bit-identical across every pairwise comparison: zero changed pixels. The d3
simulation had settled before capture, the fixed browser context and fonts
rendered stably, and full-page capture was deterministic. The remaining
variable was the boundary between container-generated references and GitHub
runner comparisons: runner image changes to fonts, FreeType, or the graphics
stack can shift text rasterization enough to cross the strict pixel cap.

**Correct form.** Use the `Visual baselines` workflow. Dispatch it on an open
PR head when an intentional UI change needs reviewed references. On relevant
main pushes, the workflow first checks the committed references; only the
changed-pixel drift signature may trigger regeneration, and regeneration opens
a dated PNG-only PR. Any other test failure propagates without rewriting
references. The comparison algorithm, per-channel delta, and 0.1% cap remain
unchanged.

**Lintability — not lintable (operational).** Rendering equivalence depends on
the runner image and browser stack. The workflow makes the environment a
structural boundary; unit tests guard failure classification, complete
candidate generation, and the PNG-only mutation rule.

## Appendix: CI/QA ruling adopted 2026-09-01 21:01

**Provenance.** User ruling of 2026-09-01 21:01, adopted in full (items A-E).
Items A-C appear verbatim below in faithful English translation; D is this
document and E is summarized. The source ruling was in Chinese. It is binding
on CI/QA practice.

### A. Rerun cap

When the same test fails on two consecutive attempts, a third automatic rerun
is forbidden and the queue auto-freezes. A case recording registration and
attribution (regression or flake) must be filed before the QA line or P0 lead
may release it.

### B. Mandatory deflake

When one test flakes three or more times in one day, a mandatory deflake PR is
required. It must contain a root-cause fix; a rerun that writes the failure off
does not count.

### C. Serial-group admission and exit

Admission requires an unknown root cause and at least two failures in one day.
Temporarily mark the test `flaky` so it runs in the backend serial bucket as a
quarantine. A test exits only after a root-cause fix, including a genuine
pinning test, has merged and it has passed five consecutive runs.

### E. Rerun registration automation

Every run with attempt greater than one, including one that eventually succeeds, is automatically registered; no run may close silently.

For the conventions hierarchy and evidence-to-rule discipline, see
[defensive patterns](defensive-patterns.md) and
[documentation maintenance](doc-maintenance.md).
