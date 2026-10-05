# Vitest at pre-push; defer automatic local test subsets

## Decision and rule

On the post-#3282 base `8053a2f88`, move `frontend-vitest` to pre-push and
include it in the existing per-tool lock/load guard. Retain its full-suite
command, ts/tsx `files:` trigger and `pass_filenames: false`. CI retains its
explicit coverage runner, coverage gate, serial flaky lane and quarantine gate.

The rule was MOVE iff all three held: the suite materially hurt commit latency,
a net saving existed for a day batching commits into pushes, and CI retained an
explicit runner. All held under the measured workload model below. A one-commit,
one-push workflow gains faster commits but no total check-time saving.

## Evidence and projection

Two authorized one-shot measurements on a shared Linux host with 16 logical
CPUs, Node v22.23.2 and npm 10.9.8:

| Local command | Wall time | Result |
| --- | ---: | --- |
| `npm ci` (dependencies initially absent) | 9.07s | 834 packages; node_modules 728844 KiB |
| `npx --no-install vitest run`, first run | 22.71s | 167 files, 3392 tests passed |
| `npx --no-install vitest run`, second run | 22.95s | 167 files, 3392 tests passed |
| guarded `next typegen` + `tsc --noEmit` | 17.58s | passed |
| guarded `npm run lint` | 42.77s | passed, 20/20 baseline warnings |

Commands ran sequentially, using `/usr/bin/time -v`; the two Vitest commands
ran from `ui/web`. Typecheck and lint used the exact hook entries in
`.pre-commit-config.yaml`. Load before tests was 2.61/2.25/2.24; host contention
was uncontrolled. The old ~1.5s hook comment was stale: mean wall time was
22.83s, 15.22 times that claim. Vitest excludes its configured flaky directory
locally; CI runs that lane separately. Installation is a one-time setup cost.

The frontend push stack measured 60.35s; adding the measured Vitest mean projects
83.18s (+22.83s, +37.83%). For C matching commits and P matching pushes,
the saving is `(C-P)*22.83s`: five commits / one push gives 174.50s -> 83.18s,
a 91.32s saving. One-to-one is neutral; more pushes than commits costs more.
This is a workload scenario, not measured developer activity. Other hooks,
Python checks, added guard startup and lock contention are outside this
projection; a guard can wait up to 120s or visibly skip. Two observations are
not a latency distribution or an SLA.

CI data came from `gh api` on
`repos/zhiyuan-zhang0206/Ava/actions/workflows/ci.yml/runs?status=completed&per_page=30`,
then `actions/runs/<id>/jobs?per_page=100`. The snapshot ended at run 35924109833.
These are the latest ten completed runs with an executed frontend Vitest step,
including runs failing elsewhere. Durations are `completed_at - started_at`,
with integer-second API precision; CI Vitest includes `--coverage`.

| Run | Typegen s | tsc s | ESLint s | Vitest + coverage s |
| --- | ---: | ---: | ---: | ---: |
| [35924109833](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35924109833) | 0 | 14 | 38 | 60 |
| [35923906756](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35923906756) | 1 | 19 | 56 | 91 |
| [35923001138](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35923001138) | 1 | 18 | 51 | 79 |
| [35922553156](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35922553156) | 1 | 18 | 51 | 79 |
| [35921642851](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35921642851) | 0 | 12 | 33 | 58 |
| [35918606255](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35918606255) | 1 | 14 | 41 | 61 |
| [35917548853](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35917548853) | 0 | 20 | 54 | 90 |
| [35915071875](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35915071875) | 0 | 20 | 54 | 90 |
| [35912039116](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35912039116) | 1 | 21 | 58 | 92 |
| [35910953223](https://github.com/zhiyuan-zhang0206/Ava/actions/runs/35910953223) | 0 | 21 | 55 | 91 |

Excluded skipped frontend jobs: 35923927185, 35922200221, 35920744976, 35919414731, 35917836996, 35914421734, 35912515446. They are absent observations, not zero-duration tests.

All ten Vitest steps passed. Vitest+coverage min/median/max was 58/84.5/92s;
tsc 12/18.5/21s; ESLint 33/52.5/58s. The coverage-free local timings and CI
coverage timings are separate populations, not a hardware comparison.

## Affected-test selection assessment (task #4626, D2)

The retired local script mapped Python top-level directories to test directories;
it ran full frontend Vitest, ESLint and tsc. There is no current framework
pytest hook to accelerate. Hook `files:` / `types:` filters already decide
whether a check runs, while `pass_filenames: false` keeps whole-project checks
inside a triggered hook. Those filters do not establish test dependency closure.
For example, config/lock/CSS or external HTML changes can fall outside the
current frontend test trigger; this decision does not claim that trigger is
complete or narrow it further.

| Option | Benefit | Cost / correctness limit | Verdict |
| --- | --- | --- | --- |
| Keep hook-level file triggers and full checks; choose development tests manually by dependency | Avoids irrelevant tool invocations and repeated commit runs without a second selector | Matching pushes retain ~83.18s projected frontend work | Use now |
| Run only changed tests or map source directories to pytest directories | Cheap mapping; potentially much shorter selected runs | Misses cross-directory consumers, helpers, shared contracts and tree-scan tests; no closure argument | Reject as an automatic gate |
| Vitest related/changed dependency selection | Could save part of the measured ~22.83s | Needs validated diff bases, deleted/renamed files, global inputs, dynamic imports and non-import dependencies; startup remains | Defer pending proof |
| Pass changed files to ESLint / tsc | ESLint's ~42.77s is the larger opportunity | The warning checker accepts subsets, but config/custom rules and baseline edits need a full scan; selection must preserve the warning gate. Single-file tsc loses project configuration and cross-file checking | Keep whole-project checks |
| Reuse CI's backend selector locally | Reuses an existing conservative policy | It deliberately sends shared/unmapped inputs to FULL, contrary to bounded local-test policy; fixtures, resources and push-range semantics need separate design | Keep CI ownership |

TypeScript already enables incremental checking in `tsconfig.json`; retaining
project-wide semantics does not require inventing a file selector.

Concrete non-import edges already exist: `localstorage-policy.test.ts` scans
the frontend source tree, `color-scheme.test.ts` reads CSS/layout via filesystem
calls, `gate-login.test.ts` reads backend HTML, and `event-fixtures.test.ts`
loads JSON fixtures. An import graph alone cannot certify these tests' input
closure. As #3282 demonstrated for codegen selection, a matching-path rule
needs a separately tested completeness premise; a green subset is not proof
that an omitted consumer was irrelevant. CI's conservative backend selection
and full Trunk merge-tree net remain independent safeguards, not evidence
that a new local selector is complete.

Recommendation: implement only the small, tested Vitest stage migration now.
No selector mechanism, retired-script revival, or CI selection change.
Follow-up trace **#4626/D2-selection-proof**, deferred for operator adjudication:
collect a representative diff/timing corpus; specify global and filesystem
inputs, transitive consumers, rename/deletion handling and full-check behavior
for unknowns; compare shadow subsets against full checks with deliberately
omitted edges that must fail. Measure net savings including selection overhead
before proposing an enforcing local policy. No local full pytest fallback is
authorized by this recommendation.
