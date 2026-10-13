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
  `debt prune`.
- `debt diff --against <ref> --on-growth fail|warn` compares the recorded
  totals per code with those at a git ref. Totals, not directories, are
  compared, so debt that moves with its file does not count as growth. The
  caller chooses `fail` or `warn`; there is no default.

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
