# Backend test selection

## Purpose

Backend CI uses two layers. A Trunk merge-tree branch (trunk-merge/ or
trunk-temp/) always runs the full backend suite. A real pull request runs
either the full suite or, when the selector returns SELECTED and the workflow
is in enforce mode (the default), a direct-import-selected subset in its place.
The merge queue therefore continues to verify the combined tree with its full
regression net; test selection does not change broken-main risk.

The selector is [scripts/ci/test_selector.py](../../../scripts/ci/test_selector.py). It is
stdlib-only and builds a direct static import reverse map for the checked-out
tree. It does not execute tests, import application code, modify the checkout,
or infer dynamic imports.

The existing e2e-env-guard job is outside this selection path. It continues to
run its complete tests/e2e/ package plus tests/harness/test_home_isolation.py in one
serial process whenever either side changes; no selector output feeds it.

## Decision rules

The first matching rule decides the outcome. FULL keeps the full backend suite;
SKIP keeps the existing documentation-only behavior (no backend suite); only
SELECTED replaces the backend pytest fan-out, and only in enforce mode.

| Order | Changed-path or event condition | Result |
| --- | --- | --- |
| 1 | Not a pull_request, or head ref begins trunk-merge/ or trunk-temp/ | FULL (queue-or-non-pr) |
| 2 | Every path is a documentation path | SKIP |
| 3 | A path is under base/, ava/, agent/, ava_builtins/, db/, or migrations/ and is not inside a `tests/` directory | FULL (the report names the forced root) |
| 4 | A path is pyproject.toml, .test_durations, or any conftest.py | FULL |
| 5 | A path is under tests/e2e/ | FULL |
| 6 | A path is neither a current collectable backend test, a direct-map source key, nor documentation | FULL (unmapped) |
| 7 | Otherwise, union direct importers of changed mapped sources with changed collectable test files | candidate subset |
| 8 | The candidate is empty | FULL (no-tests) |
| 9 | Candidate estimated time exceeds 80% of the full backend estimate | FULL (subset-too-close) |
| 10 | None of the above | SELECTED |

Tree-scan tests join the candidate subset before rules 8-10 run: every
`test_lint_*.py` under `tests/` (any depth, non-e2e) and the repo-level
CI/governance checks pinned in `scripts/ci/test_selector.py`
(`_TREE_SCAN_TESTS`). The direct-import map cannot reach a repo-wide scan
test from a changed source file, and a green subset must not miss a
tree-wide gate (task #4183: PR #3020's subset passed while the full
population was red on tests/contracts/test_lint_event_kinds.py). Name a new scan test
`test_lint_*.py` to join automatically, or extend `_TREE_SCAN_TESTS`;
scripts/tests/test_test_selector.py guards completeness and staleness.

The documentation predicate reuses base.deploy.git.repo_change.is_doc_path, the same
owner as CI's frontend/backend classifier. It recognizes the existing project doc
axes and top-level Markdown, plus `.ava.okf.md` files in a component's `docs/`
layer, including under scripts/ and ui/web/. Test data in a `tests/` directory
does not qualify as a component document. Other nested Markdown, including
AGENTS.md and SKILL.md, retains its code-directory classification.

Files under schedules/ and any `tests/` directory (the top-level one or a package's
own `<pkg>/**/tests/`) remain conservative in the selector. The classifier's
existing schedule policy is unchanged. For Trunk PRs, CI always enables the backend
side before invoking the selector; its forced FULL rule therefore remains reachable
even on a documentation diff. Non-PR runs enable both sides. The independent
documentation, language, content-manifest and security gates run on documentation
PRs too.

Tests live in the top-level `tests/` or beside the code they prove in
`<pkg>/**/tests/` (hosts: agent, ava, ava_builtins, base, cli, gateway, ops, scripts,
services). A unit test sits in the `tests/` directory of the package it tests; an
integration test across packages sits in the lowest package that may legally import
everything it uses; end-to-end tests and contract tests that read repository artifacts
stay in the top-level `tests/` ([testing guide](../../../tests/README.md#where-to-put-tests)).
The selector treats both alike: a test-only edit under `base/x/tests/` is a
test change resolved through the reverse map (rule 3 does not force FULL for it), and
a module that merely carries a `test_` prefix outside a `tests/` directory
(`scripts/ci/test_selector.py`) is not a test.

## Static map and blind files

The map AST-parses every Python file under any `tests/` directory, except files named
conftest.py, and walks imports in every scope. It includes both module imports
and absolute from-import targets; for example, from agent.execution import child as exec_child
reaches agent/execution/child.py, and from base import lm reaches
base/lm/__init__.py when those paths exist. Relative imports and unresolved
modules are omitted.

Only importer files named test_*.py or *_test.py outside tests/e2e/ are
collectable. Test helpers are still inspected but do not add selected tests.
Resolution considers these source roots: agent, ava, cli, gateway, ops,
services, base, ava_builtins, ui, scripts, and schedules.

This is intentionally a direct static map, not a coverage claim. About 280 of
roughly 970 source files have no static test reachability, including
agent/nodes.py, agent/mcp_daemon.py, and ava/sdk_surface/ files. A changed blind
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
[.test_durations](../../../.test_durations) file, refreshed nightly by
[refresh-test-durations.yml](../../../.github/workflows/refresh-test-durations.yml).
It maps pytest node IDs to seconds. Refreshes retain every measured node,
including fast tests and durations that round to zero; only unmeasured nodes
use pytest-split's average fallback. The selector sums entries whose node ID
starts with each selected test file plus ::; a file with no timing entry costs
the average present backend timing entry. A recorded zero remains zero. The
same model estimates the complete collectable backend universe, and only
subsets at or below 80% run.

## Selection modes and artifacts

`TEST_SELECTION_MODE` in [ci.yml](../../../.github/workflows/ci.yml) is the single
switch. The `test-select` job republishes its value as a job output — job-level
routing (`if`, `continue-on-error`) cannot read `env`, only `needs` — and every
routing expression keys off it.

**enforce** (the default since 2026-09-11):

- A SELECTED real PR runs `backend-selected` instead of the shard fan-out: the
  selected subset is the backend pytest gate, and the aggregator (the
  branch-protection check `backend (pytest + pyright)`) requires it.
- `backend-selected` uses the native pytest result and the shared JUnit
  validation gate, like the shards. Test failures remain red; no external
  quarantine service or secret determines the verdict.
- The aggregator still requires static / structure / serial flaky / pgvector
  smoke / helper signing exactly as before.
- No coverage artifacts are produced on that path: the 85% full-tree coverage
  gate stays on the full fan-out — every Trunk merge-tree branch runs one, as
  does every non-SELECTED PR.
- `test-select` remains non-gating: a selector or setup failure leaves its
  outputs empty, which routes every consumer down the full-suite path.

**shadow** (the revert switch):

- No PR gates on the subset. The full fan-out gates exactly as before the
  enforcement switch; the subset runs informationally; the
  `test-selection-shadow-report` job records the divergence comparison — FALSE
  GREEN only when a passing subset meets a failing full non-flaky pytest
  population.

Trunk merge-tree branches never take the enforced path: rule 1 returns FULL,
so the fan-out runs regardless of the mode.

## Maintenance and the revert switch

- Review any false green immediately and close a real blind-map gap; no false
  green is accepted as a known exception.
- Keep selector unit tests focused on observable decisions, AST resolution,
  current-test filtering, duration estimates, queue branches, and
  deterministic JSON.
- Refresh .test_durations through its normal nightly workflow after material
  suite changes.
- Revert: set `TEST_SELECTION_MODE: "shadow"` in ci.yml (one line) and update
  the enforce-default assertion in tests/scripts/test_ci_test_selection.py.
  The wiring contracts deliberately turn red when the switch moves — that
  tripwire is what keeps a revert from silently losing a gate.

## History: the shadow window and the enforcement decision

Shadow mode ran from 2026-09-03 (its introduction) through 2026-09-10. Its
comparison artifacts recorded 978 distinct CI runs: 716 on pull requests (464
distinct PRs) and 262 push runs, where the selector does not run and the
report is a no-op. Decisions: FULL 680 (forced roots and unmapped paths
dominated), SELECTED 31 runs / 25 PRs, empty 267 (262 push runs plus a handful
of concurrency-cancelled runs). One run recorded FALSE GREEN (PR #1842,
2026-09-06): triage attributed it to a time-dependent assertion in
a daemon test in `tests/components/services/` — unrelated to that PR's diff, and
fixed the same morning by #1840 (merged five minutes after this run's decision
was recorded). Its decision payload had no changed blind file, so no static-map
gap was involved. No other false green was observed, and no informational
false-negative (subset red while the full population passed) either.

2026-09-10 23:26 user ruling: switch to enforcement directly; the earlier
staged criteria (a 100+ PR window with a monthly re-measure, every false green
fixed as a blind-map gap) are not a precondition for this switch. They stay
recorded here as history, and the false-green rate remains the review signal
while enforce is live.
