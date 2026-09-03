# Backend test selection

## Purpose

Backend CI uses two layers. A real pull request keeps the full backend suite
that branch protection already requires, and, in shadow mode, also runs a
direct-import-selected subset as an informational measurement. A Trunk
merge-tree branch (trunk-merge/, trunk-temp/, or mergify/merge-queue) always
uses the full suite. The merge queue therefore continues to verify the combined
tree with its full regression net; test selection does not change broken-main
risk.

The selector is [scripts/test_selector.py](../scripts/test_selector.py). It is
stdlib-only and builds a direct static import reverse map for the checked-out
tree. It does not execute tests, import application code, modify the checkout,
or infer dynamic imports.

The existing e2e-env-guard job is outside this selection path. It continues to
run its complete tests/e2e/ package plus tests/test_home_isolation.py in one
serial process whenever either side changes; no selector output feeds it.

## Decision rules

The first matching rule decides the outcome. FULL means the informational
subset is not useful for this change; the existing full backend suite still
runs. SKIP means the change is documentation-only. SELECTED contains a sorted
backend test-file list.

| Order | Changed-path or event condition | Result |
| --- | --- | --- |
| 1 | Not a pull_request, or head ref begins trunk-merge/, trunk-temp/, or mergify/merge-queue | FULL (queue-or-non-pr) |
| 2 | Every path is a documentation path | SKIP |
| 3 | A path is under shared/, ava/, agent/, ava_builtins/, db/, migrations/, or evals/ | FULL (the report names the forced root) |
| 4 | A path is pyproject.toml, .test_durations, or any conftest.py | FULL |
| 5 | A path is under tests/e2e/ | FULL |
| 6 | A path is neither a current collectable backend test, a direct-map source key, nor documentation | FULL (unmapped) |
| 7 | Otherwise, union direct importers of changed mapped sources with changed collectable test files | candidate subset |
| 8 | The candidate is empty | FULL (no-tests) |
| 9 | Candidate estimated time exceeds 80% of the full backend estimate | FULL (subset-too-close) |
| 10 | None of the above | SELECTED |

The documentation predicate reuses shared.repo_change.is_doc_path. Files under
scripts/, schedules/, and tests/ are deliberately not treated as documentation
by the selector even when their name ends in Markdown: operational schedule and
test changes must remain conservative.

## Static map and blind files

The map AST-parses every Python file under tests/, except files named
conftest.py, and walks imports in every scope. It includes both module imports
and absolute from-import targets; for example, from agent import exec_child
reaches agent/exec_child.py, and from shared import lm reaches
shared/lm/__init__.py when those paths exist. Relative imports and unresolved
modules are omitted.

Only importer files named test_*.py or *_test.py outside tests/e2e/ are
collectable. Test helpers are still inspected but do not add selected tests.
Resolution considers these source roots: agent, ava, cli, gateway, ops,
services, shared, ava_builtins, evals, ui, scripts, and schedules.

This is intentionally a direct static map, not a coverage claim. About 280 of
roughly 970 source files have no static test reachability, including
agent/nodes.py, agent/mcp_daemon.py, and ava/_exports/ files. A changed blind
file is unmapped and forces FULL; it never silently produces an empty or
optimistic subset. Dynamic imports, reflection, subprocess boundaries, and test
helpers are also reasons to prefer the full net.

The map is rebuilt for every run; no map artifact is committed. Its blind-file
set therefore moves with the tree: a newly unmapped changed file is reported as
unmapped and takes the full-suite path. String-based module access and other
dynamic imports stay outside this map and conservatively force full. There is
no coverage-derived map yet; that is a future option, not an enforcement claim.

## Duration guard

The timing input is the repository-root
[.test_durations](../.test_durations) file, refreshed nightly by
[refresh-test-durations.yml](../.github/workflows/refresh-test-durations.yml).
It maps pytest node IDs to seconds. The selector sums entries whose node ID
starts with each selected test file plus ::; a file with no timing entry costs
the average present backend timing entry. The same model estimates the
complete collectable backend universe, and only subsets at or below 80% run.

## Shadow status and artifacts

TEST_SELECTION_MODE is currently shadow in
[ci.yml](../.github/workflows/ci.yml). A selected real PR therefore runs:

- test selection, which prints the JSON decision used as the audit trail;
- backend selected subset (shadow), which records every pytest exit status
  without failing its job and uploads test-selection-subset-log;
- test selection shadow report, which records the existing full backend result
  and compares the subset to backend-shard, the complete non-flaky pytest
  population that uses the same marker filter, then uploads
  test-selection-shadow-report.

The report emits FALSE GREEN only when the selected subset passes but the full
non-flaky pytest population fails. Static, structure, coverage, pgvector, and
flaky-serial failures remain visible through the unchanged backend aggregator,
but cannot be attributed to selection. A subset failure with a successful full
non-flaky pytest population is an informational false-negative: expand the
static map or identify why the full suite alone covers that change.

All three shadow jobs use job-level failure containment, so selector, setup,
artifact, and report faults are informational too. None of their check names is
in branch protection or .trunk/trunk.yaml. The existing backend aggregator and
all required checks remain the only gates.

## Maintenance and an enforce switch

Review a false-green immediately and close its blind-map gap; no false-green is
accepted as a known exception. Keep selector unit tests focused on observable
decisions, AST resolution, current-test filtering, duration estimates, queue
branches, and deterministic JSON. Refresh .test_durations through its normal
nightly workflow after material suite changes.

Move real PRs from shadow to enforce only in a separate PR, after all of these
conditions hold:

1. The false-green rate is below 1% over at least 100 PRs, re-measured monthly.
   The initial stronger target is either 100 consecutive PRs with zero
   false-greens, or fewer than one false-green per 500 PRs. Record the measured
   numerator, denominator, and observation window here as data accumulates.
2. Every observed false-green has been fixed as a blind-map gap; none remains
   classified as an accepted known cause.
3. Trunk merge-tree branches remain full regardless of any real-PR enforcement
   decision.

This change adds shadow measurement only. An enforcement change would require
its own review of branch protection, job dependencies, observed report data,
and this document.
