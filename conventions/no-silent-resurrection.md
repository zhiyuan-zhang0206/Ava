# no-silent-resurrection

## What this is

`no-silent-resurrection` is a required pull-request check: the job
`no silent resurrection` in `.github/workflows/ci.yml` (listed in
`.trunk/trunk.yaml`'s `merge.required_statuses`) runs
`scripts/ci/no_silent_resurrection.py`, which compares the lines a PR adds
with the lines `main` deleted in the last 30 days. A match fails the check
unless a commit in the PR carries a `Resurrects: <reason>` line or reverts the
deleting commit. The job is PR-only (a push event has no base branch),
runs on every non-draft PR and on the Trunk test branches, and, like
`repo-language`, ignores the change classifier: a docs-only PR can carry
deleted lines back too.

## Why

On 2026-10-04, #4245 (6ecfe14c3) deleted the delivery-watchdog field code and
#4207 (151bc92fe) landed after replaying a stale branch through a conflict
resolution, carrying the old content of four of those files back onto main.
The resurrected code compiled and its tests passed: every existing gate was
green, because every gate judges the tree as it stands, not the content the
tree already stopped carrying. "Re-check your rebase" is a rule, and rules are
not a mechanism; this check is. The decision record is
`decisions/2026-10-04-no-silent-resurrection.md`.

## How it works

Implementation: `scripts/ci/no_silent_resurrection.py` (stdlib + git only).

1. Added lines come from `git diff -U0 -M <merge-base> <head>` and are split
   into runs of consecutive added lines.
2. A line is a **candidate** when it is "strong": not blank, not a
   comment/import/pure punctuation, between 14 and 1000 characters, and
   carrying at least two identifiers of three or more characters. Weak lines
   (comments, imports, blanks, brackets, short generic lines, and any line
   over 1000 characters - data blobs) never fail the check; they only bridge
   runs.
3. **Alive** candidate lines - still present anywhere in the base tree
   (skipped paths excluded) - are not dead: a move does not hide a
   resurrection, and a line that still exists is not resurrected.
4. `main` history is scanned once, streaming (`git log --since=<N>days
   --no-merges -p -U0 <base>`). A commit **deletes** a candidate line when its
   diff removes it from a non-skipped path and the commit does not re-add the
   text somewhere in the same commit (a move is not a deletion).
5. Inside a run, a stretch where every strong line is dead - weak, blank and
   still-present lines bridge; a strong line that is neither breaks the
   stretch - is a **hit** when it holds at least three dead strong lines or one
   distinctive dead line (an identifier of at least 20 characters with an
   underscore or camelCase). A hit is attributed to the commit covering the
   most of its dead lines (ties: the most recent).
6. **Allowances** come from the PR's commit messages (merge-base..head): a
   `Resurrects: <reason>` line allows every hit, or only the hits it names by
   deleting sha (at least 7 hex characters, containing a digit) or path
   (containing `/` or a file extension); `This reverts commit <sha>` allows
   the hits that sha deleted. A declaration that matched no hit is warned
   about.

Skipped paths are never candidates, never deletions, and are excluded from the
alive scan - a resurrected generated file is triggered by its source, which
this check does see: `uv.lock`, `ui/web/package-lock.json`,
`ui/web/openapi.json`, `ui/web/src/lib/types-generated.ts`,
`base/host/env/config_lite_table.json`, `base/events/registry.md`,
`db/schema.sql`, `scripts/zombie_pyright_ignores.registry`, the generated
`api.txt` files under `base/agents` and `base/events` (and their
pre-2026-09-29 `shared/` layout), `scripts/structure/baseline/`, and
`migrations/`.

Exit codes: 0 clean or fully allowed, 1 blocking hits, 2 usage or git failure.

## Allowing a resurrection on purpose

A deliberate revert or restore costs one commit-message line in the PR:

    Resurrects: bring back the watchdog fields

Name the deleting sha or a path to allow only the matching hits:

    Resurrects: 6ecfe14c3
    Resurrects: services/delivery_watchdog/

A `Resurrects:` line with neither a sha nor a path allows every hit; a commit
that reverts the deleting commit (`This reverts commit <sha>`) is allowed
automatically. When the check fails it prints the recipe with the actual
deleting sha and path of the first hit; it warns about declarations that
matched no hit, which should be removed.

## Matching is textual

A rename-and-edit of a deleted function under a new name is not caught. Lines
are compared after stripping leading and trailing whitespace; no other
normalization happens, so a reformatted copy of deleted content may match or
not match depending on the whitespace. A line that only moved within `main`
is not dead (it still exists in the base tree), and a line a commit removed
and re-added in the same commit was never deleted.

## False positives and tuning

The thresholds (`MIN_STRONG_LENGTH`, `MAX_STRONG_LENGTH`, `MIN_DEAD_LINES`,
`DISTINCTIVE_IDENTIFIER_LENGTH` in the script) are set against a sweep of the
check over every `main` commit of the 30-day window (one pass = 2024 commits).
When the check flags unrelated content, record the commit here and either
extend the skip table, raise the minimum unit, or tighten the line classifier;
the sweep is the tool for that. Recorded passes and residual classes:

- Pass 1 (2 dead lines / solo identifier >= 12): 116 of 850 commits flagged
  (~13.7%). The noise was single "distinctive" hits on generic lines
  (`raise psycopg.OperationalError("db down")`, `capture_output=True` -
  framework names are not distinctive) and two-line idioms (`def main() ->
  int:` + `parser = _parse()`); the incident's true blocks were all 3+ lines.
- Change: minimum unit 2 -> 3 dead lines; the solo-line identifier bar
  12 -> 20 characters.
- Pass 2 (after the change): numbers recorded in a follow-up commit before
  the first enqueue.
- 2026-10-04, the check's own introduction: the skip table names the
  pre-2026-09-29 `shared/` locations; those literals are text the shared ->
  base move (9ae6105a6) had deleted. Under pass-1 thresholds the check
  flagged its own PR and a sha-scoped `Resurrects:` line demonstrated the
  allowance; the pass-2 thresholds drop the two-line match below the minimum
  unit and the trailer was removed.

## Local runs

    python3 scripts/ci/no_silent_resurrection.py --base origin/main --head HEAD

`--days N` changes the window; `--base`/`--head` accept any refs, so the check
can be pointed at a single commit (`--base <sha>^ --head <sha>`).
