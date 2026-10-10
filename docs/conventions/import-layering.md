# Import layering

Which top-level package may import which, how that is enforced, and what the
enforcement does not see. Read before adding an import that points "up", and when
`lint-imports` refuses one.

**Source of truth.** The contracts in `pyproject.toml` (`[tool.importlinter]`).
This page says what they mean; where the two differ, the contracts win. Direction
between modules of one package (`base.x` against `base.y`) is not governed here.

## The stack

`base < ava < agent < gateway < cli`: a lower layer never imports a higher one. A
higher layer may import any lower layer, not only the next one down. A chain counts
like a direct import, including a chain through a module outside the stack
(`base` → `ava_builtins` → `cli` fails).

`gateway` and `cli` do not import each other. The stack puts `cli` above `gateway`,
which bars `gateway` → `cli`; the ops contract below makes the two independent
siblings, which bars `cli` → `gateway`. What both need sits below them, in `base`
(`base.api_contracts`) or `ops`.

## The contracts

One row per contract in `pyproject.toml`, under the name it has there.

| Contract | Rule |
|---|---|
| `Layered architecture (base < ava < agent < gateway < cli)` | the stack above |
| `Hosted agent runner (base < ava < agent < services.agent_runner.agent_host)` | `services.agent_runner.agent_host` sits above `agent`; nothing below it imports `services.agent_runner.agent_host` |
| `services must not import the agent kernel` | `services` does not import `agent`, except the enumerated `ignore_imports` entries ([below](#ignore_imports)) |
| `base must not import services` | `base` does not import `services`; no exceptions |
| `services must not import cli` | `services` does not import `cli`; no exceptions |
| `Ops layer (base < ops < {gateway, cli})` | `ops` imports `base` but not `gateway` or `cli`; `base` does not import `ops`; `gateway` and `cli` are independent siblings |

## Packages outside the stack

- **`services`** is not a layer. The contracts fix four things: `base` does not
  import it; it does not import `cli`; it does not import `agent`, except the
  enumerated entries (all but one are `services.agent_runner.agent_host`, the hosted runner that
  runs agent turns in-process and sits above `agent`); and nothing below `agent`
  imports `services.agent_runner.agent_host`. That puts `services` below `cli`, above `base` and
  outside the agent kernel. No contract names a direction between `services` and
  `gateway`, `ava` or `ops`.
- **`ops`** has only the ops contract: above `base`, below `gateway` and `cli`.
  Nothing relates it to `ava`, `agent` or `services`; `ops` and `services` import
  each other ([known legacy](#known-legacy)).
- **`ava_builtins`** (built-in plugins and skills) is in the graph but in no
  contract of its own. It imports `base`, `ava`, `agent`, `ops` and `services`, and
  `agent` and `ops` each import back into it (`agent` and the plugins are cyclic by
  design). Chains through it count for every contract above.
- **Not in the graph at all**: `scripts/`, `tests/`, `schedules/` and `demos/`, the
  Python outside `root_packages`. Nothing checks what they import or what imports
  them.

## What the check sees

- Every `import` statement in the eight `root_packages`, function-local ones
  included: a lazy `from cli... import` inside a service fails like a top-level one.
- Chains of imports, not only direct pairs. The report prints the whole chain with
  line numbers.
- Not string-form imports (`importlib.import_module("cli....")`, `__import__`).
- Not most test code. The top-level `tests/` is not a root package. A package's own
  `tests/` directory has no `__init__.py`, and a directory without one below a
  package that has one is not in the graph, so a test there can import upward
  without failing a contract. In the graph: a test file placed directly in a package
  directory, and test directories under `services` directories that themselves lack
  an `__init__.py` (`services` is a namespace package). The tests-location check does
  not yet judge whether a test sits in a package that may import what the test uses
  ([`tests-location`](../../scripts/lint/docs/tests-location.ava.okf.md)).

## Known legacy

Edges the contracts do not see or do not cover. They are listed, not exempted.

- `services` → `scripts`: `services/backup/scheduler/worker.py` and
  `services/backup/walg/drill.py` import
  `scripts.data_plane_ops.restore_drill` inside functions. Production code importing
  a tooling script; `scripts/` is outside the graph.
- `ops` ↔ `services`: `ops/roster/__init__.py` and `ops/spec.py` import `services.*`
  inside functions, and many `services` modules import `ops`. No contract covers the
  pair.
- String-form imports of a higher layer. `base/sessions/helperproc.py` loads
  `services.desktop.permissions_helper.client` by name: a `base` → `services` edge, which
  `base must not import services` exists to forbid. `ava/__init__.py` and
  `ava/external/state.py` load `agent.extensions` and `agent.state` by name;
  `ava/__init__.py` documents that as how `ava` keeps no static dependency on
  `agent`.

## Checking

```bash
.venv/bin/lint-imports --cache-dir .cache/import-linter
```

The `lint-imports` hook runs it on every commit that stages a `.py` file; it
analyses the whole graph (`pass_filenames: false`), in a few seconds. CI runs it with
the other pre-commit hooks over all files in the `backend structure` job, and again
on the merged tree (`merged-tree-structure`, informational). A violation prints the
import chain.

The contracts also feed the legacy private-patch home calculation in
`scripts/structure/placement.py`. Root-test admission instead uses the complete
subject-directory LCA in `scripts/structure/placement_evidence.py`; import direction
does not choose the owner of those subjects. Existing registered paths retain
their policy, and package-local placement is not yet enforced.

### Shared dependency evidence

`scripts/structure/imports/facts.py` owns direct dependency facts for tooling,
using normalized import clauses and `placement.ModuleIndex` for exact checkout
resolution. Absolute and relative imports, local imports and aliases use the
same resolver. A missing first-party module remains an explicit unknown; it
cannot silently become an existing parent package. `from pkg import symbol`
depends on the package door unless the named submodule exists. Following that
door's own imports belongs to a consumer's dependency closure.

The collector also recognizes literal dynamic imports, imported `unittest.mock.patch`
string targets, bounded literal pytest
string parameters and f-strings, actual Python `-m`/`-c` launches, and literal
paths anchored by `Path(__file__)` inside the checkout. Lexical bindings
prevent an unrelated parameter or comprehension target from shadowing another
scope's import or path. Patch target facts retain their callee, so ownership may
prune patch-only subjects while runtime impact keeps the import dependency.
`patch.dict` and `patch.multiple` object forms do not invoke the string importer.
The collector proves standard-library mappings and explicit module imports through
lexical bindings; a `from` submodule target also requires a namespace or inert
package door. Imported symbols that could be strings, rebound attributes and opaque
targets retain diagnostics. Existing static imports of object targets remain edges.
It does not execute Python, infer arbitrary builders or prove runtime branch
coverage. Resource facts describe possible referenced paths; directory facts
give a conservative subtree, not proof that every child was read. Known paths
remain facts when the target is absent, including negative existence checks.
Unsupported recognized import and execution inputs retain their source location,
kind and reason in `Evidence.unknown`.
Recognized `Path` or `open` reads whose repository or external anchor cannot be
proved also retain an unknown, rather than treating a relative working-directory
path as repository-rooted. Source embedded in Python `-c` uses the same collector;
it has no implicit relative-import package or source-file resource anchor.
The lexical fact pass also identifies recognized launchers. Source with none
skips the second execution-input pass; launcher-bearing source retains the full
execution grammar and its unknown diagnostics. Within one execution-input query,
a transparent helper declaration and lexical parent share one shape analysis
and reuse the body scope already constructed by the visitor. Each caller still
resolves its own source argument and unknown independently; no helper or scope
cache survives the query or crosses source files.

These facts do not replace import-linter's graph or enable new placement gates.
CI reverse impact follows unpruned runtime dependencies; test ownership remains
a separate query with its existing legacy subject policy. In particular, fixture
execution dependencies do not automatically define a test's business owner.
The pure declarative reader in `imports/fixture_scopes.py` is shared by the
pytest plugin and CI; their execution and selection policies remain separate.

### Shared dependency evidence

`scripts/structure/imports/facts.py` owns direct dependency facts for tooling,
using normalized import clauses and `placement.ModuleIndex` for exact checkout
resolution. Absolute and relative imports, local imports and aliases use the
same resolver. A missing first-party module remains an explicit unknown; it
cannot silently become an existing parent package. `from pkg import symbol`
depends on the package door unless the named submodule exists. Following that
door's own imports belongs to a consumer's dependency closure.

The collector also recognizes literal dynamic imports, imported `unittest.mock.patch`
string targets, bounded literal pytest
string parameters and f-strings, actual Python `-m`/`-c` launches, and literal
paths anchored by `Path(__file__)` inside the checkout. Lexical bindings
prevent an unrelated parameter or comprehension target from shadowing another
scope's import or path. Patch target facts retain their callee, so ownership may
prune patch-only subjects while runtime impact keeps the import dependency.
It does not execute Python, infer arbitrary builders or prove runtime branch
coverage. Resource facts describe possible referenced paths; directory facts
give a conservative subtree, not proof that every child was read. Known paths
remain facts when the target is absent, including negative existence checks.
Unsupported recognized import and execution inputs retain their source location,
kind and reason in `Evidence.unknown`.
Recognized `Path` or `open` reads whose repository or external anchor cannot be
proved also retain an unknown, rather than treating a relative working-directory
path as repository-rooted. Source embedded in Python `-c` uses the same collector;
it has no implicit relative-import package or source-file resource anchor.
The lexical fact pass also identifies recognized launchers. Source with none
skips the second execution-input pass; launcher-bearing source retains the full
execution grammar and its unknown diagnostics. Within one execution-input query,
a transparent helper declaration and lexical parent share one shape analysis
and reuse the body scope already constructed by the visitor. Each caller still
resolves its own source argument and unknown independently; no helper or scope
cache survives the query or crosses source files.

These facts do not replace import-linter's graph or enable new placement gates.
CI reverse impact follows unpruned runtime dependencies; test ownership remains
a separate query with its existing legacy subject policy. In particular, fixture
execution dependencies do not automatically define a test's business owner.
The pure declarative reader in `imports/fixture_scopes.py` is shared by the
pytest plugin and CI; their execution and selection policies remain separate.

## `ignore_imports`

Only `services must not import the agent kernel` has an exemption list. The other
five contracts have none, so an edge one of them refuses is removed by moving code,
not by an exemption. The list:

- is enumerated per target module rather than written as `agent.**`, so importing
  anything new from the kernel is a contract failure that has to be argued for;
- cannot rot: an entry that stops matching an import fails the run, so a dropped
  import forces its entry out;
- has its diff as the review signal.

## When an import is refused

A primitive that both sides need moves down to the lowest layer both may import,
usually `base`, and both import it from there (a data-plane path, a listener probe or
a URL validator that a daemon and `cli` both need lives in `base`). `ignore_imports`
is for the argued exception, not the way out.
