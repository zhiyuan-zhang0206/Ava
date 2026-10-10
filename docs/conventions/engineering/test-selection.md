# Backend test selection

## Purpose

Backend CI uses two layers. A Trunk merge-tree branch (trunk-merge/ or
trunk-temp/) always runs the full backend suite. A real pull request runs
either the full suite or, when the selector returns SELECTED and the workflow
is in enforce mode (the default), an owner-rule-selected subset in its place.
The merge queue therefore continues to verify the combined tree with its full
regression net, and a gap in the PR-side subset surfaces in the queue instead
of reaching main; test selection does not change broken-main risk.

The selector is [scripts/ci/test_selector.py](../../../scripts/ci/test_selector.py). It is
stdlib-only and consumes the shared runtime dependency facts in
`scripts.structure.imports.facts`. It follows imports transitively through application
modules, test helpers and imported tests, and adds the actual pytest fixture bindings:
root/local conftests, literal `pytest_plugins`, and the shared `path_scopes.toml` reader.
Each path-scoped binding retains its TOML source as an input, including removed base
declarations; a missing first-party fixture module reports incomplete impact at that source.
Relative imports, finite dynamic imports, literal Python subprocess modules/code and
recognized repository-rooted resource paths use that same evidence owner and module resolver. Placement
subject policies do not prune this runtime impact graph. The selector never imports
application code or executes test code.

Global fixture dependencies can therefore reach most tests. This is conservative
runtime coupling: the duration guard reports `subset-too-close` and keeps FULL rather than
removing those edges to produce a smaller subset. An opaque dynamic input on a test's
reachable dependency graph produces `incomplete-impact`, with source locations and
reasons in the JSON `diagnostics` field and stderr. Explicit edges do not certify that
an unrelated opaque input is resolved. Syntax errors, malformed declarations and
unexpected analysis errors fail the selector job and the required backend check.

Documentation is also an input when the shared resource graph proves a possible reader.
The workflow's classify job and the selector query the same head/base evidence before
skipping a documentation-only diff. Inputs with known readers request backend CI;
unrelated unknowns alone do not turn an unread documentation change into a runtime edge.
Classification analysis errors fail the required backend check as well. Each classify
invocation keeps its base and changed-path evidence in a private temporary directory,
removed on success or failure; concurrent invocations cannot replace each other's inputs.

The existing e2e-env-guard job is outside this selection path. It continues to
run its complete tests/e2e/ package plus tests/harness/test_home_isolation.py in one
serial process whenever either side changes; no selector output feeds it. The
e2e shards, hosted e2e and e2e-env-guard jobs run for every diff that is not
documentation-only (the `classify` job marks any non-documentation path outside
ui/web/ as backend), so a tests/e2e/ change never depends on the backend subset
to be exercised.

## Path classes

`classify_path` gives every changed path exactly one class; `select_tests` and
the tracked-tree completeness test share it, so the rules exist once. The first
matching row decides.

| Order | Path | Class | Contribution |
| --- | --- | --- | --- |
| 1 | A documentation path (see below) | DOCUMENTATION | runtime readers from either tree |
| 2 | Root `conftest.py` | GLOBAL | full suite |
| 3 | Any other `conftest.py` (also when deleted) | CONFTEST | every collectable test below its directory plus runtime consumers |
| 4 | Absent from the head tree | DELETED | base-tree impact; FULL with a diagnostic when base facts are unavailable |
| 5 | A collectable test file | TEST | itself and its transitive test consumers |
| 6 | A global path (below) | GLOBAL | full suite |
| 7a | `.github/`, `.agents/`, `.ava/`, `.trunk/` | TREE_SCAN_ONLY | tree-scan tests plus every collectable test whose source names that top-level directory (below) |
| 7b | `demos/`, `tests/e2e/`, `.pre-commit-config.yaml`, `.gitignore`, `.gitattributes`, `.gitleaks.toml`, `LICENSE`, `NOTICE`, `.test_durations`, `.test_durations.source.json` | TREE_SCAN_ONLY | tree-scan tests plus recognized runtime consumers |
| 8 | A non-Python file under `ui/` | FRONTEND | recognized backend resource consumers, when backend selection runs; the frontend job also owns it |
| 9 | Any other file under agent/, ava/, ava_builtins/, base/, cli/, gateway/, ops/, schedules/, scripts/, services/, tests/ or ui/ | PACKAGE | transitive runtime consumers plus the owning package's tests |
| 10 | Anything else | UNMAPPED | full suite |

Global paths apply to every test: `pyproject.toml`, `uv.lock`, `.python-version`,
`.env.example`, the root `conftest.py`, the modules its `pytest_plugins` lists
(plus the `__init__.py` of every package on their import path), and everything
under tests/fixtures/, db/, migrations/, deploy/ and commands/. This list and
the plugin set are the single "global path" concept in the selector.

Tests read `.github/`, `.agents/`, `.ava/` and `.trunk/` by path. Recognized
resource facts reach their transitive consumers. A change there selects, besides the tree-scan tests, every
collectable test whose source contains the directory as a string literal: the
unquoted `.github/` (not preceded by a word character or a dot, so the module
name `base.agents` does not match `.agents`) or the quoted `".github"` or
`'.github'`. Test sources are scanned at selection time; no map file is stored.

The owning package of a path is found by walking up from its directory: the
nearest `tests` directory that holds at least one collectable test owns it, and
every collectable test below that directory is the package's test set. The
directory itself counts when the path is already inside a `tests` directory,
otherwise a `tests` directory beside an ancestor counts. A path with no nearer
owner falls back to the top-level tests/ directory. A file's transitive runtime
consumers are always added to its package tests.

CI passes the committed merge-base through `--base-ref`. Selection reads both base
and head with the same fact collector and resolver, then intersects the resulting tests
with the current collectable universe. This preserves impact when an import, fixture
binding or resource edge is removed, and when a changed source is deleted. Base facts
are read from a temporary Git archive, never from an unrelated working checkout.
Without base facts, a deleted path cannot certify a subset and reports `incomplete-impact`.

## Decision rules

The first matching rule decides the outcome. FULL keeps the full backend suite;
SKIP keeps the existing documentation-only behavior (no backend suite); only
SELECTED replaces the backend pytest fan-out, and only in enforce mode.

| Order | Changed-path or event condition | Result |
| --- | --- | --- |
| 1 | Not a pull_request, or head ref begins trunk-merge/ or trunk-temp/ | FULL (queue-or-non-pr) |
| 2 | Every path is documentation and has no known runtime reader in either tree | SKIP |
| 3 | A path is GLOBAL | FULL (`global-path:<first path>`; the payload lists every global path) |
| 4 | A path is UNMAPPED | FULL (unmapped; the payload lists the paths) |
| 5 | Reachable dynamic input or missing deleted-path base facts | FULL (incomplete-impact; diagnostic locations and reasons) |
| 6 | Otherwise, union the contribution of every path with the tree-scan tests | candidate subset |
| 7 | The candidate is empty | FULL (no-tests) |
| 8 | Candidate estimated time exceeds 80% of the full backend estimate | FULL (subset-too-close) |
| 9 | None of the above | SELECTED (owner-tests) |

Rule 4 is a runtime safety net. scripts/tests/test_test_selector_owner_rules.py
classifies every tracked path and fails when any is UNMAPPED, so the trunk never
reaches rule 4; a new top-level directory or root file needs a class in the
selector before it can merge.

Tree-scan tests join the candidate subset before the duration and empty-candidate rules run: every
`test_lint_*.py` under `tests/` (any depth, non-e2e) and the repo-level
CI/governance checks pinned in `scripts/ci/test_selector.py`
(`_TREE_SCAN_TESTS`). The owner rules cannot reach a repo-wide scan
test from a changed source file, and a green subset must not miss a
tree-wide gate (task #4183: PR #3020's subset passed while the full
population was red on tests/contracts/test_lint_event_kinds.py). Name a new scan test
`test_lint_*.py` to join automatically, or extend `_TREE_SCAN_TESTS`;
tests/scripts/test_test_selector_contract.py guards completeness and staleness.

The documentation predicate reuses base.deploy.git.repo_change.is_doc_path, the same
owner as CI's frontend/backend classifier. It recognizes the existing project doc
axes and top-level Markdown, plus `.ava.okf.md` files in a component's `docs/`
layer, including under scripts/ and ui/web/. Test data in a `tests/` directory
does not qualify as a component document. Other nested Markdown, including
AGENTS.md and SKILL.md, retains its code-directory classification.

Files under schedules/ and any `tests/` directory (the top-level one or a package's
own `<pkg>/**/tests/`) are never documentation: they are PACKAGE paths. The classifier's
existing schedule policy is unchanged. For Trunk PRs, CI always enables the backend
side before invoking the selector; its forced FULL rule therefore remains reachable
even on a documentation diff. Non-PR runs enable both sides. The independent
documentation, language, content-manifest and security gates run on documentation
PRs too.

Tests live in the top-level `tests/` or beside the code they prove in
`<pkg>/**/tests/`. The host directories come from
`tool.pytest.ini_options.testpaths` in `pyproject.toml`, the same owner used by
pytest collection, the pyright test-environment generator and the fixture-scope lint.
Supported entries are the literal `tests` and `<host>/**/tests`; missing configuration
or unsupported patterns fail rather than silently narrowing a tool's scope. A unit test sits in the `tests/` directory of the package it tests; an
integration test across packages sits in the lowest package that may legally import
everything it uses; end-to-end tests and contract tests that read repository artifacts
stay in the top-level `tests/` ([testing guide](../../../tests/README.md#where-to-put-tests)).
The selector treats both alike: a test-only edit under `base/x/tests/` is a
TEST path that runs itself, a helper or data file there belongs to that
package's tests, and a module that merely carries a `test_` prefix outside a
`tests/` directory (`scripts/ci/test_selector.py`) is not a test.

## Runtime impact facts and limits

The dependency graph AST-parses Python files under the shared code roots and `tests`,
including non-collectable helpers and conftests. Each collectable non-e2e backend test
is a graph root. Module edges also include concrete package initializers that Python
executes during import. Dependency cycles terminate through a visited set. A helper
or imported test affects its transitive test consumers even when those consumers live
outside its nearest ownership bucket.

The shared fact collector keeps line numbers, edge kinds and unresolved expressions.
Rooted resource paths are evidence of possible use, not proof of a read or runtime
coverage; directory paths conservatively affect changed descendants.
It supports bounded static evidence, not arbitrary execution or reflection. Known
external modules have no repository input to select. Unresolved dynamic inputs remain
explicit unknown evidence. Fixtures registered through `path_scopes.toml` create real
edges, but an opaque call elsewhere in a plugin remains unknown unless its finite input
domain is proved. This can require FULL on the current repository; it is not evidence
that the runtime dependency closure is small or complete.

The graph is rebuilt for every run and no coverage-derived map is committed. Package
ownership and tree-scan rules remain conservative additions. The complete Trunk and
non-PR suites remain protected, including when ordinary PR selection is incomplete.
A selector `tests` list contains candidate files, not an executed test count: the
selected native lane omits the independently executed static lane and flaky tests,
while the serial lane owns flaky execution. JUnit records remain the execution evidence.

## Duration guard

The timing input is the repository-root
[.test_durations](../../../.test_durations) file, refreshed after 20 main changes with a nightly fallback by
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
- A selector or setup failure leaves routing outputs empty, which retains the
  full-suite path, and also fails the required backend aggregator. A parser
  failure cannot become a successful FULL decision.

**shadow** (the revert switch):

- No PR gates on the subset. The full fan-out gates exactly as before the
  enforcement switch; the subset runs informationally; the
  `test-selection-shadow-report` job records the divergence comparison — FALSE
  GREEN only when a passing subset meets a failing full non-flaky pytest
  population.

Trunk merge-tree branches never take the enforced path: rule 1 returns FULL,
so the fan-out runs regardless of the mode.

## Maintenance and the revert switch

- Review any false green immediately and close a real rule gap; no false
  green is accepted as a known exception.
- Keep selector unit tests focused on observable decisions, AST resolution,
  current-test filtering, duration estimates, queue branches, per-class owner
  rules, the tracked-tree completeness guard, and deterministic JSON.
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
