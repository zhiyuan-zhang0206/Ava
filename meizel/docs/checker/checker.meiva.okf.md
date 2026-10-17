---
type: doc
title: "meizel/ — Locality Checker"
description: "Meizel models the repository as a nested dependency DAG declared in meizel.toml manifests and checks every first-party code dependency against it: commands, fact edges, the rule, restricted Python and failure conditions."
tags:
  - locality
  - tool
---

# meizel/ — Locality Checker

## What is it

Meizel judges one question: may file `f` use file `t`? Every directory declares
a small DAG over its direct children in `meizel.toml`; the checker derives the
facts from source and fails when a fact is not covered by a declaration. The
decision and its rationale live in
[the accepted decision](../../../docs/decisions/engineering/design/simplification/2026-10-11-meizel-nested-dag-locality.md).

The checker uses only the standard library and never imports the source. The
node universe is `git ls-files -z`; facts come from tracked first-party `.py`
files. Run from the repository root.

```
python -m meizel check [paths... | --changed files...] [--limit N]
python -m meizel debt [--json] | record <paths...> | prune | carry | diff --against <ref> --on-growth fail|warn
python -m meizel explain <path>
python -m meizel sync [--prune] [--declare [paths...]]
python -m meizel bootstrap --out <dir>
python -m meizel roles list <role>... | check | write
```

`--repo <root>` selects another checkout; `--jobs N` sets parser processes. Exit codes: 0 clean, 1 violations, 2 the tool could not run.

## Commands

- `check` is the gate. The whole graph is analysed; `paths` narrow what is
  reported. Each diagnostic gives `file:line`, the fact edge, the directory `L`
  that judges it, and either the manifest line to add or the public door to use.
  `check --changed files...` judges only those files.
- `debt` manages recorded violations, a per-directory baseline that may only
  shrink: [[meizel/docs/checker/debt.meiva.okf.md|recorded debt]].
- `explain` prints a node's place in its parent's local graph and the fact
  edges that cross its boundary.
- `sync --prune` drops stale declarations; `sync --declare` writes the legal new
  dependencies of the changed files: [[meizel/docs/checker/sync.meiva.okf.md|keeping manifests current]].
- `bootstrap --out DIR` writes one `meizel.toml` per directory under `DIR` from
  the current fact edges (keeping every non-edge table), plus
  `DIR/report.json`: edge counts, transitive-reduction sizes, strongly connected
  components with a shortest cycle and a witness fact per edge, cycles that need
  test files, exports blocked by underscore children, and violation counts by
  code. The repository is untouched.

## Fact edges

- `import` and `from ... import` at any nesting level. Relative imports resolve
  against the importing file's directory. `from pkg import name` targets the
  submodule `name` when `pkg/name.py` or `pkg/name/` exists, otherwise
  `pkg/__init__.py` with `name` recorded as a used name. Resolution follows
  `pythonpath = ["."]`: a dotted name maps to a tracked path, directories
  without `__init__.py` are namespace packages.
- Attribute use on an imported module (`mod.name`, `pkg.sub.name`), through any
  submodule chain.
- pytest fixture requests: test and fixture parameters without defaults,
  `usefixtures`, and `getfixturevalue("name")`, resolved to the defining file by
  pytest's order: the file itself, its literal `pytest_plugins`, then each
  ancestor `conftest.py` and its plugins. Parametrized names and `patch`-injected
  arguments are not requests.
- Object-form patching: `monkeypatch.setattr(mod, "name", ...)`,
  `mock.patch.object(mod, "name")`, `patch.multiple(mod, name=...)` use `name`
  in `mod`.
- `[loads]` declarations (below); `path_scopes.toml` mounts
  ([[meizel/docs/checker/mounts.meiva.okf.md|mounts]]).

## The rule

For a fact edge `f -> t`, `L` is the deepest directory containing both; `cf` and
`ct` are the children of `L` holding `f` and `t`.

1. `L`'s `[uses]` declares `cf -> ct`.
2. Every directory from `ct` down to `t`'s parent exports the next child on the
   path. `__init__.py` is the door, no export needed; a child whose name starts
   with an underscore (not a dunder) can never be exported.
3. Every name used from `t` is public. A leading underscore (dunders excepted)
   makes a name private to its defining file, tests and fixtures included: a
   fixture requested from another file has a public name.

Local graphs are acyclic. A test's use of a fixture from an ancestor
`conftest.py` is contained in the tree and needs no declaration. Importing
`a.b.c` initialises `a` and `a.b`; that is loading and creates no edge.

## Restricted Python

Binding resolution finds these through aliases (`from importlib import
import_module as im`): `importlib.import_module`, `__import__`,
`runpy.run_module`, `runpy.run_path`, `importlib.util.spec_from_file_location`,
`exec`, `compile`, a Python subprocess that runs anything but a literal file or module
([[meizel/docs/checker/python-commands.meiva.okf.md|commands]]), a `pytest_plugins` that is not a literal, string patch
targets that name first-party code (`mock.patch("a.b")`, `monkeypatch.setattr("a.b", v)`;
stdlib and third-party targets are fine, an unreadable target is not), and
writing a first-party module into `sys.modules` by name (`sys.modules["a.b"] = ...`,
`del`, `pop`, `setdefault`, `update`, `monkeypatch.setitem/delitem`, `patch.dict`).
Reading `sys.modules` is not restricted. A file that owns a dynamic load declares it:

```toml
[loads]
"loader.py" = ["meiva_builtins"]   # repository-relative directories
```

Each entry becomes a fact edge from the file to the directory, judged by the
same rule; the file is then exempt from the restricted-form errors. A loading
file should do only the loading.

A file that runs code from outside the repository (user scripts, agent code, a
module named by operator configuration) declares it under `[external]` with a reason:

```toml
[external]
"runner.py" = "executes the schedule script the user wrote"
```

The file is then exempt from `exec`, `compile` and the dynamic-import forms; none
makes an edge. Other restricted forms (`sys.modules` writes, python commands,
string patches) still apply. The reason must be non-empty; an entry whose file
has none of the exempt forms is `stale-external`.

Plugin and schedule-script discovery is `[external]`, never `[loads]`.

## Manifest

`meizel.toml` keys are `exports` (list), `[uses]` (endpoints may name deeper paths:
[[meizel/docs/checker/narrowing.meiva.okf.md|narrowed endpoints]]), `[loads]` and `[external]`
(only for sites listed in [[meizel/docs/checker/policy.meiva.okf.md|the policy file]]) and
`[debt.<code>]`. Names are direct children: files with extension, directories by name.
`meizel.toml` is not a child and does not count toward the directory budget. A directory
without a manifest has no local edges and exports its door.

## When it fails

Diagnostic codes: `undeclared-edge`, `not-exported`, `private-module`, `private-name`,
`cycle-edge`, `restricted-python`, the patch codes and `missing-patch-target` ([[meizel/docs/checker/patching.meiva.okf.md|patching]]),
`unresolved-import`, `unlisted-allowance`,
`stale-load`, `stale-external`, `policy-syntax`, `debt-paid`, and the manifest codes
(`manifest-` + syntax, unknown-key, unknown-child, duplicate, self-edge, cycle, export-private,
stale-edge, debt, roles). An undeclared edge that would close a cycle says so; the way out is
a public door or injection.

## Layout

`cli.py` parses commands; `analysis.py` assembles universe, scans, manifests and
diagnostics; `universe.py` reads git; `scan.py`, `scopes.py` and `fixtures.py`
extract facts from one file; `manifest.py` validates manifests; `rule.py`
projects and judges; `graphs.py` holds components and cycles; `explain.py`,
`sync.py`, `bootstrap.py` and the debt files implement the other commands.
Tests build throwaway git repositories in `tests/`.

Parent: [[meizel/docs/meizel.meiva.okf.md|meizel]].
