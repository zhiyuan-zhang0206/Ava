---
type: doc
title: "scripts/meizel/ — Locality Checker"
description: "Meizel models the repository as a nested dependency DAG declared in meizel.toml manifests and checks every first-party code dependency against it: commands, fact edges, the rule, restricted Python and failure conditions."
tags:
  - scripts
  - locality
  - tool
---

# scripts/meizel/ — Locality Checker

## What is it

Meizel judges one question: may file `f` use file `t`? Every directory declares
a small DAG over its direct children in `meizel.toml`; the checker derives the
facts from source and fails when a fact is not covered by a declaration. The
decision and its rationale live in
[the accepted decision](../../../docs/decisions/engineering/design/simplification/2026-10-11-meizel-nested-dag-locality.md).

The checker uses only the standard library and reads source without importing
it. The node universe is `git ls-files -z`; facts come from tracked first-party
`.py` files. Run from the repository root.

```
python -m scripts.meizel check [paths...] [--limit N]
python -m scripts.meizel explain <path>
python -m scripts.meizel sync --prune
python -m scripts.meizel bootstrap --out <dir>
```

`--repo <root>` selects another checkout; `--jobs N` sets parser processes
(default up to 8). Exit codes: 0 clean, 1 violations, 2 the tool could not run
(git failure, unparsable source, unknown path).

## Commands

- `check` is the gate. The whole graph is analysed; `paths` narrow what is
  reported. Each diagnostic gives `file:line`, the fact edge, the directory `L`
  that judges it, and either the manifest line to add or the public door to use.
- `explain` prints a node's place in its parent's local graph (declared to use,
  declared to be used by, exported or not) and the fact edges that cross its
  boundary in both directions.
- `sync --prune` removes declared edges and `[loads]` entries that no fact
  needs, repeating until nothing stale remains. Comments outside the edited
  lists survive. A manifest left empty is deleted. Adding an edge is always a
  manual manifest edit.
- `bootstrap --out DIR` writes one `meizel.toml` per directory, mirrored under
  `DIR`, from the current fact edges, plus `DIR/report.json`: per-directory edge
  counts, transitive-reduction sizes, strongly connected components with a
  shortest cycle and one witness fact per edge, cycles that need test files,
  exports blocked by underscore children, and violation counts by code. The
  repository is left untouched.

## Fact edges

- `import` and `from ... import` at any nesting level. Relative imports resolve
  against the importing file's directory. `from pkg import name` targets the
  submodule `name` when `pkg/name.py` or `pkg/name/` exists, otherwise
  `pkg/__init__.py` with `name` recorded as a used name. Resolution follows
  `pythonpath = ["."]`: a dotted name maps to a tracked path, directories
  without `__init__.py` are namespace packages.
- Attribute use on an imported module (`mod.name`, `pkg.sub.name`), including a
  deeper submodule reached through the chain.
- pytest fixture requests: test and fixture parameters without defaults,
  `usefixtures`, and `getfixturevalue("name")`, resolved to the defining file by
  pytest's order: the file itself, its literal `pytest_plugins`, then each
  ancestor `conftest.py` and its plugins. Parametrized names and arguments
  injected by `patch` decorators are not requests.
- Object-form patching: `monkeypatch.setattr(mod, "name", ...)`,
  `mock.patch.object(mod, "name")`, `patch.multiple(mod, name=...)` use `name`
  in `mod`.
- `[loads]` declarations (below).

## The rule

For a fact edge `f -> t`, `L` is the deepest directory containing both; `cf` and
`ct` are the children of `L` holding `f` and `t`.

1. `L`'s `[uses]` declares `cf -> ct`.
2. Every directory from `ct` down to `t`'s parent exports the next child on the
   path. `__init__.py` is the door and needs no export; a child whose name starts
   with an underscore can never be exported.
3. Every name used from `t` is public. A leading underscore (dunders excepted)
   makes a name private to its defining file, tests included.

Local graphs are acyclic. A test's use of a fixture from an ancestor
`conftest.py` is contained in the tree and needs no declaration. Importing
`a.b.c` initialises `a` and `a.b`; that is loading and creates no edge.

## Restricted Python

Binding resolution finds these through aliases (`from importlib import
import_module as im`): `importlib.import_module`, `__import__`,
`runpy.run_module`, `runpy.run_path`, `importlib.util.spec_from_file_location`,
`exec`, `compile`, `python -c` source that imports first-party code or cannot be
read as a literal, a `pytest_plugins` that is not a literal, and string patch
targets (`mock.patch("a.b")`, `monkeypatch.setattr("a.b", v)`). A file that owns
a dynamic load declares it:

```toml
[loads]
"loader.py" = ["ava_builtins"]   # repository-relative directories
```

Each entry becomes a fact edge from the file to the directory, judged by the
same rule; the file is then exempt from the restricted-form errors.

## Manifest

`meizel.toml` keys are `exports` (list), `[uses]` and `[loads]` (tables of
lists). Names are direct children: files with extension, directories by name.
`meizel.toml` itself is not a child and does not count toward the directory
budget. A directory without a manifest has no local edges and exports its door.

## When it fails

Diagnostic codes: `undeclared-edge`, `not-exported`, `private-module`,
`private-name`, `restricted-python`, `unresolved-import` (a first-party import
naming no tracked module), `stale-load`, and the manifest codes
`manifest-syntax`, `manifest-unknown-key`, `manifest-unknown-child`,
`manifest-duplicate`, `manifest-self-edge`, `manifest-cycle`,
`manifest-export-private` and `manifest-stale-edge` (a declared edge no fact
needs). An undeclared edge that would close a cycle says so; the way out is a
public door or an injected collaborator.

## Layout

`cli.py` parses commands; `analysis.py` assembles universe, scans, manifests and
diagnostics; `universe.py` reads git; `scan.py`, `scopes.py` and `fixtures.py`
extract facts from one file; `manifest.py` validates manifests; `rule.py`
projects and judges; `graphs.py` holds components and cycles; `explain.py`,
`sync.py` and `bootstrap.py` implement the other commands. Tests build
throwaway git repositories in `tests/` and run in the static test environment.

Parent: [[scripts/docs/scripts.ava.okf.md|scripts]].
