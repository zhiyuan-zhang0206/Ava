---
type: doc
title: "meizel/ — Recorded Locality Debt"
description: "Strict and strict-to-be directories: how a meizel.toml records violations a directory may keep, why the baseline can only shrink, and the debt, record, prune and diff commands."
tags:
  - locality
  - tool
---

# meizel/ — Recorded Locality Debt

## What is it

The gate cannot wait for every violation to be fixed first. A directory can
record the violations it has today in its own `meizel.toml`; `check` then
stops new ones at once and the recorded ones are repaid a step at a time. The
tool supplies the mechanism; how a repository wires it into hooks and CI is
that repository's policy and is not decided here.

## Two modes, no switch

A directory's mode follows from its baseline:

- **strict**: nothing recorded. Every violation fails.
- **strict-to-be**: something recorded. A recorded violation passes, a new one
  fails, and a recorded debt that no longer occurs also fails (`debt-paid`,
  repaid by `debt prune`). The baseline can therefore only go down, and a
  directory whose baseline reaches zero is strict again.

There is no lenient mode and no per-directory switch. A violation in a
strict-to-be directory says so: it names the directory, how many debts it
carries, and that the fix is to repair the violation, since `debt record` is for
deliberate exceptions.

## The baseline is local

A baseline sits in the directory that judges the violation, like a declared
edge, so concurrent repayments touch different files:

- an edge violation (`undeclared-edge`, `not-exported`, `private-module`,
  `private-name`, `cycle-edge`) belongs to the lowest common directory `L` of
  the fact edge;
- `restricted-python` belongs to the directory of the file.

```toml
[debt.private-name]
"test_a.py -> a.py#_helper" = 3

[debt.restricted-python]
"loader.py#importlib.import_module" = 1

[debt.cycle-edge]
"x/m.py -> y/n.py" = 2
```

Paths are relative to the owning directory. A key carries no line number, so
moving code inside a file leaves the baseline alone; the count is how many
occurrences are allowed. Tables are sorted and one debt takes one line, so a
repayment is a one-line diff. `[debt]` does not count toward the directory
budget. `cycle-edge` records the facts on the declared edges that close a cycle.
The edges are the ones some entry projects onto; the declared graph without them
must be acyclic, or `manifest-cycle` fails. Each fact on such an edge counts
like any other: a new import file, or another import line, in the pair is a new
violation, so the recorded facts can only shrink. When an edge closes no cycle
any more, or is no longer declared, its entries are `debt-paid`. A malformed
`[debt]` is `manifest-debt`. Unresolved imports, stale declarations and manifest syntax
errors cannot be recorded.

## Commands

- `debt` reads only the manifests (no analysis, about a second) and prints the
  totals by code, by top-level directory and the number of strict-to-be and
  strict directories; `--json` for tools.
- `debt record <paths...>` writes the current violations under the paths into
  their owners' manifests, creating a manifest where there is none (stage it:
  `check` reads tracked files). It is the only way a baseline grows: `check`
  and `bootstrap` never write one, and `record` never lowers a count. Cycle
  back-edges are chosen greedily and thinned to the ones needed, and the facts on
  them are recorded. Violations
  that cannot be recorded are printed and the exit code is 1.
- `debt prune` lowers counts and drops entries to what still occurs.
- `debt carry [--base ref]` follows a move. A baseline names files, so a
  `git mv` leaves the old entries paid and the new place unrecorded. For the
  files git detects as renamed since the branch point with `origin/main`
  (default; the move must be staged, and a heavily rewritten file may not be
  detected), it relocates entries whose source or target was renamed, to the
  owner and key they now have. It only relocates: counts merge, the total never
  changes, and a new violation in the moved file stays an error. Run it, then
  `debt prune`. `--rename-target OLD=NEW` rewrites a patch target that was
  renamed. `--move-symbol OLD_FILE::NAME=NEW_FILE` follows a top-level function
  or class that moved to another file, which git cannot see as a rename (a
  fixture moved into a helper module). It scans the symbol's lines in the old
  file at the base and in the new file, and rekeys one for one at most what the
  symbol cost before, costs now and has recorded, so what it gained on the way
  stays an error. The old file must no longer define the name.
- `debt sites --code <patch code> [--target-prefix P]... [--json] [paths...]`
  lists where the source holds each patch of a code: file, line, form and
  target, sorted by target. A prefix is dotted (`lib.mod` holds `lib.mod.f`, not
  `lib.mod2`), several are a union, and paths limit the scan. It reads the
  source, not the baseline, so it lists what a codemod has to change whether or
  not it is recorded. Codemods and design write-ups use it instead of their own
  scripts.
- `debt diff --against <ref> --on-growth fail|warn` compares the recorded
  totals per code with those at a git ref. `--by-target` adds the recorded patch
  debt per target group: what the change paid down or added, to paste into a PR
  description. Entries are counted by the target in their key, so debt that
  moves with its file stays in its group. The groups are the first `--depth`
  dotted names of a target (default 3), or the `--target-prefix` values (a target
  counts under the longest one, the rest under `(other)`; a listed prefix always
  shows). Totals, not directories, are
  compared, so debt that moves with its file does not count as growth. The
  caller chooses `fail` or `warn`; there is no default.
- `--waive-if-changed <pathspec>...` marks a change that edits those paths (the
  rule code) as a rule change, which is judged in two parts
  (`meizel/contracts/debt_rules.py`). The growth the new rules cause is what this
  checker, run on a checkout of the ref, records for the ref's unchanged code, minus what the ref
  records: reported, never judged. The growth the content causes is what the change records
  beyond that: judged. Every recorded key must be one the new rules find in the ref, with no
  greater count, so a declaration cannot hide a new violation and a violation cannot ride along.
  A file moved in the same change fails; move files separately. A manifest the checker cannot
  read in the ref stops the comparison. Other changes are judged by totals, with no waiver.

## Depths of `check`

- `check --changed files...` is the pre-commit depth. It reads every manifest
  (so the declared graphs are checked in full), scans only the named Python files
  plus the conftests and plugins that fixture resolution needs, and judges those
  files' fact edges and restricted forms, every `[loads]` declaration, and the
  recorded debt about the named files. It does not look for stale declarations,
  and the effect of a manifest edit on files that did not change waits for the
  full run.
- `check` analyses the whole repository, including stale declarations and
  cycles. It is the depth for pre-push and CI.
- `debt diff` is the extra CI step against the merge base.

Parent: [[meizel/docs/checker/checker.ava.okf.md|the checker]].
