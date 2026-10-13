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
- writing a first-party module into `sys.modules` by name (assignment, `del`,
  `pop`, `setdefault`, `update`, `monkeypatch.setitem/delitem`,
  `patch.dict`). Reading `sys.modules` is allowed.

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

A file that runs code from outside the repository (user scripts, agent code,
a module named by operator configuration) is declared under `[external]` with
a reason instead: `"runner.py" = "executes the user's schedule script"`.
`exec`, `compile` and the dynamic-import forms are then allowed in that file and
make no edge. An `[external]` entry whose file has none of those forms is stale.

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

The checker lives in `meizel/`.

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

## Addendum (2026-10-13): strict-to-be baselines

The Decision above turns the gate on "for the whole repository at once, after
those cycles are removed". Measured on 2026-10-13 the repository is far from
that point: about 20 directories still have cycles, more than 5,000 uses of an
underscore name cross files (93% of them in tests), and about a hundred
underscore-module and a dozen restricted-Python violations remain. Waiting for
zero lets new debt accrue while the old is repaid. This addendum adds a middle
state that lets the gate open now, stops new debt at once, and makes the debt
only go down. The rule itself is unchanged.

### Two modes, derived from the baseline

A directory's mode follows from what its baseline records; there is no switch
and no lenient setting.

- **strict**: the baseline is empty. Every violation fails.
- **strict-to-be**: the baseline still holds entries. A recorded violation
  passes, a new one fails, and a recorded entry that no longer occurs fails too,
  with the instruction to run `meizel debt prune`. The baseline can only
  shrink, and when it reaches zero the directory is strict again.

A failure in a strict-to-be directory says so in plain words: the directory
carries N recorded debts that must only shrink, the violation should be fixed
rather than recorded, and `meizel debt record` exists for deliberate exceptions.

### The baseline is as local as the rule

There is no repository-wide baseline: concurrent repayments would all edit one
file. Each directory records its own debt in a `[debt.<code>]` table of its
`meizel.toml`, owned by the directory that judges the violation, which is where
a declared edge would go:

- edge violations (`undeclared-edge`, `not-exported`, `private-module`,
  `private-name`) belong to the lowest common directory of the fact edge;
- file violations (`restricted-python`) belong to the directory of the file;
- a cycle is recorded as the facts on the declared edges that close it
  (`cycle-edge`, counted per source and target file); the local graph without
  those edges must be acyclic.

Entries are keyed by code, source, target or name, with a count and no line
number, so moving code does not rewrite the baseline. Tables are sorted and one
debt takes one line. `[debt]` is not an entry of the directory budget.
The 800-line file ceiling and the 20-entry directory cap are not meizel
diagnostics today; when they become one, they take the same two owners (the
file's directory, and the parent of the directory that is too full).

### Commands and the three depths of `check`

- `meizel debt` reads the manifests only and prints counts by code, by
  top-level directory and by mode (`--json` for tools). It does not analyse the
  source, so it answers in about a second. It is where "how much debt is left"
  is answered.
- `meizel debt record <paths...>` writes the current violations under the paths
  into their owners' baselines. It is the only way a baseline grows; `check` and
  `bootstrap` never write one.
- `meizel debt prune` lowers the baselines to what still occurs.
- `meizel debt diff --against <ref> --on-growth fail|warn` compares the recorded
  totals per code with a git ref. It compares totals, not directories, so debt
  that moves with its file is not growth.
- `check --changed <files...>` is the cheap depth for pre-commit: it reads all
  manifests (so every declared graph is checked for cycles), scans only the named
  files and what fixture resolution needs, and judges their fact edges and the
  recorded debt about them, without the repository-wide stale checks. `check` is
  the whole-repository depth, with stale declarations, for pre-push and CI.

### Mechanism and policy are separate

The tool provides mechanisms: `record` can raise a baseline, and `diff` leaves
fail-or-warn to its caller (the flag is required). How a repository wires the
depths into hooks and CI, and whether growth fails or warns, is repository
policy and is decided with the gate that turns meizel on, not here.

## Addendum (2026-10-13): patching first-party code is a diagnostic

The Decision says that tests "inject collaborators or patch through the object
form", and keeps the object forms legal. That is withdrawn. A test that needs
`monkeypatch` or `patch` to replace first-party code shows a dependency that was
not injected: the code under test reaches its collaborator through a module or
class attribute instead of receiving it. A better patch does not fix that. The
fix is structural: a public facade over an implementation whose dependencies are
explicit parameters, and a test that passes the fake in directly.

Meizel therefore reports `first-party-patch` for these calls when their target
resolves, through the file's imports, to a first-party module or object:

- `mock.patch.object`, `mock.patch.multiple` (one site per patched name) and
  `mock.patch.dict`;
- `monkeypatch.setattr` and `monkeypatch.delattr`.

String targets stay `restricted-python`, since they hide the dependency from
the checker; they are not counted twice.

**Boundary.** The rule covers first-party targets only. Patching the standard
library or a third-party package (`time.sleep`, `requests.get`, `os.environ`),
`monkeypatch.setenv`/`delenv`, `setitem`/`delitem` on first-party dicts, and
targets the file cannot resolve to an import (a class defined in the same file,
an argument, `self.attr`) are not reported. Whether to widen the rule to any of
these is a separate decision.

The diagnostic is recorded and repaid like any other. Its debt is owned by the
directory of the patching file and keyed by form and dotted target without a line
number, so the existing patches enter the baselines of their directories and the
rule fails every new one. Widening a rule is the one case in which a baseline
legitimately grows at the moment the rule is introduced; the gate's `debt diff`
step reports that growth like any other.

## Addendum (2026-10-13): cycle debt is counted per fact

The first form of cycle debt recorded the declared edges that were allowed to
close a cycle. That is a projected edge, so every new import between the two
children rode on the existing record, which is the opposite of strict-to-be.
Cycle debt is now recorded like every other: as facts. `cycle-edge` entries are
keyed by source and target file with a count, and the edges they project onto are
the back-edges, so a new file or another import line on a back-edge fails, and
the recorded facts can only shrink. The declared graph without the back-edges must
still be acyclic (`manifest-cycle`), and an edge that closes no cycle any more, or
is no longer declared, makes its entries `debt-paid`. The totals reported by
`debt` rise, since a back-edge used by many files now carries many entries; that
is the true size of the debt.

## Addendum (2026-10-14): narrowed endpoints

The declaration `a -> b` joins two children of a directory, and the rule judges a
fact by the lowest common directory of its ends. Some invariants are narrower
than that: the kernel may be used by one service and by no other. A child-level
edge cannot say it, and without a way to say it a second tool has to keep
guarding it (import-linter's `services` / `agent` contract).

An endpoint of an edge in `[uses]` may now be any tracked path below the
directory, relative to it: `"services/agent_runner/agent_host" = ["agent/process_boot.py"]`.
A fact is covered when its source lies under a declared source and its target
under that pair's target; a plain child name means the child itself, so
existing manifests are unchanged. The graph on which cycles are checked is the
projection onto direct children, so the model, the acyclicity at every level and
the declared-edge-per-directory review stay as they were. A narrowed pair that no
fact matches is stale like any other, and `sync --declare` never widens one: a
fact from a source the narrowing does not authorise stays an error for a person
to judge. This is the same idea as visibility in Bazel, applied in the directory
where the two sides meet.

## Addendum (2026-10-14): hard-coded allowances

Rules that rely on reviewers noticing are not rules. Everything a rule lets pass
by someone's say-so is now either in the baselines, which only shrink and which
CI refuses to grow, or in one file.

- **One list.** `meizel.policy.toml` holds every allowance: the `[external]` sites
  with their reason, the `[loads]` sites with their directories, and a closed,
  empty `[default-arguments]`. A manifest that uses one of these forms at a site
  the file does not list is an error (`unlisted-allowance`); the reason written in
  the manifest no longer counts. The initial content is today's sites. A change to
  the file is listed by `policy diff` in the CI log and check summary; it informs,
  it does not block.
- **Patching is debt of every kind.** The earlier boundary (patching the standard
  library, a third-party package or the environment, and targets that do not
  resolve, were not reported) is withdrawn. `stdlib-patch`, `third-party-patch`,
  `env-patch` (`monkeypatch.setenv/delenv`, `os.environ`) and `unresolved-patch`
  join `first-party-patch`, each recordable and shrink-only. An unresolved target
  is not a way around the rule: only what is already in the baseline passes.
- **Growth fails.** CI runs `debt diff` with `--on-growth=fail`. The one waiver is
  decided by path, not by a reader: a change that edits meizel's rule code (Python
  files under `meizel/` outside tests) reports growth as a warning, because
  widening a rule legitimately grows the baselines.
- **Default-argument injection** stays closed. If it is ever opened, the allowed
  values go into `[default-arguments]` and the tool checks them.

## Addendum (2026-10-14): the waiver is for pure rule changes, and the lints' exemptions join the policy

Two gaps in the hard-coded allowances, found when a rename in another line happened to edit
an allowlist inside `meizel/`.

- **The waiver is for a pure rule change.** The earlier test (the change edits Python under
  `meizel/` outside tests) let any pull request that touched one such file, for any reason, grow
  the baselines. Now both conditions must hold: the change edits meizel's rule code, and every
  file it changes is under `meizel/`, a `meizel.toml` baseline or `meizel.policy.toml`
  (`debt diff --waive-if-changed ... --waive-only-within ...`). A change that also edits product
  code or tests is not waived, so a rule change gets its own pull request and its reviewer sees at
  once what it is. Baselines and the policy are part of a rule change because widening a rule
  moves them; they alone, without rule code, are not.
- **Every exemption is in the policy.** The lint rules in `meizel/` kept their own lists of what
  they let through: the ambient-state facades, wiring and pure callees, the clock-lattice
  exemptions, the single-owner `postgres-dial` exemptions, the `os.environ` files and variables,
  the per-agent settings files, the `TYPE_CHECKING` files. They now live in `meizel.policy.toml`,
  one table per rule, each entry with its reason, and the rule modules only read them
  (`meizel/allowances.py` owns the format). `policy diff` reports a change to any of them in CI, so
  rule code holds no exemption of its own. The lists in `scripts/` outside `meizel/` are not moved
  by this change.
