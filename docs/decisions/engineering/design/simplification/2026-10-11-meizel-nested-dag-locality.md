# Meizel: locality as a nested dependency DAG

Date: 2026-10-11
Status: Accepted

## Context

Ava's locality goal is that a contributor, human or agent, can understand and
change a component by reading that component and the public interfaces of the
components it uses. The boundary has to be programmatic: a rule that a checker
enforces, not a convention someone remembers.

The previous approach inferred the rules from the code. Test placement was
computed from the lowest common ancestor of a test's subjects, private patch
authority from that placement, fixture bindings from a separate registry
(`path_scopes.toml`), component entries from a public-contract audit, and runtime
reach from a shared AST analyzer that evaluated finite literal tables, helper
forwarding, file-loader chains and `python -c` strings. Each escape found in
review produced a new evaluation form, a new rule or a new registry entry.
Measured on 2026-10-11:

- The static test selector returned FULL for all 150 sampled single-file
  changes, and every collectable test was tainted by an unresolved input.
  1634 of the 1827 unresolved inputs were ordinary unanchored file reads.
- `scripts/{structure,lint,ci}` grew from 9.7k to 27.5k source lines in two
  weeks, plus 23.8k lines of tests.
- The share of fixes touching five or more source files did not fall
  (14.3% before, 18–19% after).

The underlying problem is that an AST yields facts: which file uses which. It
cannot yield norms: which uses are allowed. Norms have to be declared. And
Python lets any module reach any other at run time, so a checker that tries to
recognize every way of doing so never finishes. The fix is to restrict what
first-party code may write until every dependency is a static fact.

## Decision

Locality is checked by **meizel**, a repository tool that models the
repository as a nested dependency DAG.

### Model

- **Nodes** are the tracked files and directories.
- **Containment edges** come from the filesystem. Every node has exactly one
  parent, so containment forms a tree, and the tree is part of the graph.
- **Local graphs.** Every directory owns a small DAG over its direct children,
  declared in that directory's `meizel.toml`. An edge `a -> b` means that
  anything inside child `a` may use child `b`.

### The rule

A **fact edge** `f -> t` means that file `f` uses file `t`. For each fact edge,
meizel finds `L`, the deepest directory containing both files. `L` is unique
because containment is a tree. Let `cf` and `ct` be the children of `L` that
contain `f` and `t`. The fact edge is legal when all three conditions hold:

1. `L`'s local graph declares `cf -> ct`.
2. `t` is exported along the path below `ct`. Every directory from `ct` down to
   `t`'s parent lists the next child on the path in its `exports`. A
   directory's `__init__.py` is its door and needs no export. A child whose name
   starts with an underscore can never be exported.
3. Every name `f` uses from `t` is public. A name with a leading underscore is
   private to the file that defines it, for tests as well as production code.

Every local graph must also be acyclic. Together with condition 1, this makes
the dependency structure acyclic at every level of the tree: between files in a
directory, between packages, and between top-level components.

Two relationships follow from containment and need no declaration:

- **Ancestor conftests.** A test's use of an ancestor directory's `conftest.py`,
  and of the fixtures it defines, behaves like a lexical scope.
- **Package initialisation.** Importing `a.b.c` executes `a/__init__.py` and
  `a/b/__init__.py`. This is loading, not use, and creates no fact edge.

### Fact edges

Meizel derives fact edges from first-party Python source without importing or
running it:

- `import` and `from ... import` at any nesting level, with relative imports
  resolved against the importing package. `from pkg import name` targets the
  submodule `name` when one exists, otherwise `pkg/__init__.py`.
- Attribute use on an imported module (`mod.name`), checked for privacy.
- pytest fixture requests, resolved by pytest's own rules to the defining
  conftest or to a module named in a literal `pytest_plugins`.
- Object-form patching: `monkeypatch.setattr(mod, "name", ...)` and
  `mock.patch.object(mod, "name")` are uses of `name` in `mod`.
- Declared dynamic loads (below).

Links between documents are navigation, not dependencies, and are not fact
edges. Runtime data inputs (files read at run time, database tables, Redis keys,
environment variables) are left to a separate decision. This decision covers
code dependencies only.

### Restricted Python

First-party code is limited to a subset in which every code dependency is a
static fact. Meizel reports these forms as errors:

- `importlib.import_module`, `__import__`, `runpy.run_module` and
  `runpy.run_path`, and `importlib.util.spec_from_file_location`;
- `exec` or `compile` of source text, and `python -c` source that imports
  first-party code;
- string patch targets: `mock.patch("a.b.c")` and
  `monkeypatch.setattr("a.b.c", value)`. Tests inject collaborators or patch
  through the object form.

Dynamic loading that a component genuinely owns, such as plugin and skill
discovery, is **declared** rather than exempted. The manifest of the directory
containing the loading file lists the directories that file may load from:

```toml
[loads]
"loader.py" = ["ava_builtins"]   # repository-relative directories
```

Meizel turns each declaration into a fact edge from the loading file to the
loaded directory, and checks that edge under the same rule. A dynamic form in a
file without a `loads` entry is an error.

### Manifest

`meizel.toml` holds one directory's local graph. Keys and values name direct
children: files with their extension, directories by name.

```toml
# base/db/meizel.toml
exports = ["handle.py", "transaction.py"]

[uses]
"handle.py" = ["connections.py", "_pool.py"]
"transaction.py" = ["handle.py"]
"tests" = ["connections.py", "handle.py", "transaction.py", "_pool.py"]
```

- A directory without a manifest has no local edges and exports only its door.
- Meizel fails on any of the following:
  - unknown keys or unknown children;
  - duplicate entries;
  - self-edges;
  - a cycle;
  - an exported underscore child;
  - a declared edge that no fact edge needs.
- Because stale edges fail, every manifest is an exact map of how that
  directory's children depend on each other. A reader can learn a directory's
  internal structure from one short file.
- `meizel.toml` does not count toward the per-directory entry budget.

### Commands

- **`meizel check`** is the gate in pre-commit and CI.
  - It uses only the standard library and imports no application code.
  - Each diagnostic names the fact edge (`file:line`), the directory `L`, and
    the exact manifest line to add, or the public door to use instead.
- **`meizel explain <path>`** shows a node's view of the graph: what it uses,
  what uses it, what it exports, and its place in its parent's local graph. It
  is the navigation entry point for people and agents.
- **`meizel sync --prune`** removes declared edges that no fact edge needs.
  Adding an edge is always an explicit manifest edit, so every new dependency
  appears in review in the directory where the two sides meet.

The checker lives in `scripts/meizel/`.

### Mechanisms it replaces

Once meizel is the gate, the following are deleted:

- test placement:
  - `scripts/structure/tests_location.py`, its allowlist and its baselines;
  - the placement inference in `scripts/structure/placement.py` and
    `scripts/structure/placement_evidence.py`;
- fixture bindings: every `path_scopes.toml` and `tests/fixtures/path_scopes.py`.
  Their bindings move to conftests.
- private access:
  - the private-patch placement inference in `scripts/lint/patch_targets.py`;
  - the cross-package private-access rule in `scripts/lint/code_structure.py`;
- public contracts: `scripts/lint/public_contracts.py` and
  `[tool.ava.public_contracts]`;
- import-linter contracts. The root and package manifests express the same
  layering.
- the evaluation machinery in `scripts/structure/imports/`: literal tables,
  helper forwarding, file-loader proofs and executed-source analysis.

The per-directory entry budget stays, because it keeps every local graph small.
Test selection consumes meizel's fact edges as its runtime input. Its policy is
a separate decision.

## Alternatives rejected

- **Inferring legality from facts** (subject LCA, placement registries,
  evidence-based private authority). This was the previous approach. A norm
  cannot be derived from what the code currently does, and each inference gap
  became another rule or registry entry.
- **A flat DAG without containment.** The lowest common ancestor is not unique
  in a DAG, so a fact edge has no single place to be judged. One repository-wide
  graph of about 3,600 files also cannot be reviewed locally.
- **The tree alone as the norm** (tree plus LCA, no declared edges). Shared code
  drifts toward the root, which is how `base` came to be reached by most tests,
  and the remaining cases need exception lists.
- **Package-level BUILD declarations, as in Bazel or Pants.** These leave
  structure inside a package to convention and require adopting a build system.
  Meizel keeps the declared-dependency and visibility idea, applies it in every
  directory, and enforces it statically.
- **Growing the analyzer to resolve dynamic forms.** Each form adds interpreter
  surface, and the set of forms has no end. Restricting the language is cheaper,
  and it terminates.
- **Requiring empty `__init__.py` files.** Package initialisation is loading,
  not use. An `__init__.py` may define and re-export names. Re-exporting a child
  whose own code uses the package door forms a cycle in the directory's local
  graph, and acyclicity rejects it. That is a project policy, not a Python
  restriction.
- **Private access for tests.** Tests exercise public behavior or inject
  collaborators. Underscore modules remain visible to their siblings, including
  a sibling `tests` directory.
- **Document links as edges.** Mutual explanation between documents is
  legitimate, and navigation does not make a document depend on another.

## Consequences

- Measured on `e07436950e`:
  - 612 directories contain Python;
  - projecting today's code yields 4,505 local edges, 3,444 after transitive
    reduction;
  - 47 directories have cycles, all at depths 1 to 3. The largest are `base`
    (16 of 18 children in one strongly connected component), `ava`, `agent`,
    `gateway` and the repository root.
- The gate turns on for the whole repository at once, after those cycles are
  removed. There is no per-directory waiver.
- Every dependency change is a manifest edit in the directory where the two
  sides meet, so review stays local.
- First-party dynamic forms are rewritten as static imports or declared loads.
  Tests that use private names or string patch targets move to public behavior
  or injection.
- Runtime data inputs remain outside the rule until their own decision.
