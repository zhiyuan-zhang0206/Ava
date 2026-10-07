# A hook's cost follows the change, not the repository

## Context

Implementation sub-agents spent a median of 42s per `git commit` (p90 83s, a third of
the commits over a minute) and 10s per `git push` (p90 100s) inside the hooks. A
commit that changed one comment cost 36s: the Python lints each declared
`pass_filenames: false` or `always_run` and scanned all ~2500 tracked Python files, so
the time was the same for a one-line edit and a 50-file refactor.

Measured per hook on that commit, nothing in it was an interpreter or a framework
cost (a Python start is 10ms, pre-commit itself under 1s). It was whole-repository
scanning repeated by independent processes: code_structure 8.6s (a `git show` per
baseline shard, 78 processes, plus an AST/complexity walk of every file), no_emoji 7s
(a Python loop over every character of the repo), time_bomb 6s (parsing every module
to build an index), loguru_format 5.5s, then a dozen lints at about a second each.
The same commits cost twice at push: the branch-diff rerun repeated the commit stage
over the branch, and the artifact and patch-target sweeps ran at every push whatever
it touched.

## Decision

A commit-stage hook's cost must follow what the commit changes.

- **Per-file lints take the changed files** (`--only FILE...`). The default scope and
  exemptions stay the lint's own, so a changed file gets the verdict a full scan gives
  it. An edit to the lint tooling, to the structure baseline or to an input the rule
  reads widens that run to the full scan. The full scan of every lint is CI's
  `pre-commit run --all-files`, the merge gate.
- **A check that needs the whole project runs only when one of its inputs changed**
  (the codegen freshness hooks' `files:` filters), not on every commit.
- **The fixed work inside a hook is removed, not tolerated:** one batched `git
  cat-file` for the baseline shards instead of a process per shard, a lazily built
  module index in time_bomb, one regex prefilter for the emoji scan, the config registry
  imported only when a test file is judged.
- **ESLint lints the changed frontend files at commit** (through the CI warning gate;
  the whole project when the lint setup changed) and the whole project at push.
- **At push, a check runs when its inputs moved.** The branch-diff rerun costs about one
  commit, so it takes no load threshold and no lock. The artifact sweep runs only the
  hooks the nested run cannot cover (whole-repository hooks whose inputs the branch only
  deleted; per-file hooks with any deleted input). The patch-target full scan runs when
  the branch touches Python.

## Alternatives rejected

- **One merged lint process.** Interpreter start is not the cost; merging would trade the
  per-hook `SKIP=` handles and failure attribution for nothing measurable.
- **A content-hash cache of per-file verdicts.** It keeps full-scan semantics at
  incremental cost, but a fresh worktree (the normal case here) starts cold, and the
  cache needs the same declaration of rule inputs `--only` already needs.
- **Moving time_bomb, code_structure or the freshness hooks to pre-push only.** CI runs
  `--all-files` at the default stage; a pre-push-only hook silently drops out of it and
  needs CI wiring plus a contract change. Making them proportional kept stages as they were.
- **Per-file ESLint alone.** A type-aware rule can report in a file that did not change
  when a type changed elsewhere (the case behind dropping a type assertion once a status
  enum lost a member); the whole-project run at push and in CI catches it.
- **`vitest related` at push.** Still deferred for the reasons in
  [the Vitest placement decision](../../engineering/tooling/2026-09-24-vitest-prepush-selection.md): tests that read
  the source tree, CSS or fixtures have non-import dependencies.

## Consequences

- A verdict that depends on a file other than the one changed (time_bomb's
  clock-threading rule across modules, a type-aware ESLint rule) can reach CI before it is
  caught locally; the full scan is the merge gate, and ESLint's whole-project run is at
  push.
- A new per-file lint joins the pattern by taking `--only` and declaring its rule inputs;
  a new whole-project hook should be filtered by `files:` to its real inputs.
- Heavy tools (pyright over the changed files, tsc, whole-project ESLint, Vitest) keep
  the per-tool lock and load threshold at push; a skip there is still never evidence the
  check ran, and CI is the cover.
