# Import layering

Which top-level package may import which, how that is enforced, and what the enforcement does
not see. Read before adding an import that points "up", and when `meizel check` refuses one.

**Source of truth.** The root `meizel.toml` (`[uses]`, and the recorded `[debt]` of the
directories). This page says what the declarations mean; where the two differ, the manifests win.
How the rule works: [the checker](../../meizel/docs/checker/checker.ava.okf.md). Direction between
modules of one package (`base.x` against `base.y`) is the manifest of that package.

## The stack

`base < ava < agent < gateway < cli`: a lower layer never imports a higher one. A higher layer
may import any lower layer, not only the next one down. In the root manifest that is the absence
of the edge: `base` declares no use of `ava`, `agent`, `gateway` or `cli`, `ava` none of `agent`,
`gateway`, `cli`, and so on up. An import against the direction is an `undeclared-edge`, and
declaring it would close a cycle in the root's local graph, which `manifest-cycle` refuses.

`gateway` and `cli` do not import each other: neither declares the other. What both need sits
below them, in `base` (`base.api_contracts`) or `ops`.

## The rules, as the root manifest states them

| Rule | In the root manifest |
|---|---|
| `base < ava < agent < gateway < cli` | no declared edge pointing up the stack |
| `services.agent_runner.agent_host` sits above `agent`; nothing below it imports it | nothing declares a use of `services`, below `cli` |
| `services` does not import the agent kernel, except the hosted runner and the targets it uses | `"services/agent_runner/agent_host" = ["agent/...", ...]`: an edge narrowed to the one source and its listed targets ([narrowed endpoints](../../meizel/docs/checker/narrowing.ava.okf.md)) |
| `base` does not import `services` | `base` declares no use of `services` |
| `services` does not import `cli` | `services` declares no use of `cli` |
| `ops` imports `base` but not `gateway` or `cli`; `base` does not import `ops`; `gateway` and `cli` are independent | no edge from `ops` to `gateway` or `cli`, none from `base` to `ops` |

A chain counts like a direct import: the local graph of every directory is acyclic, so a chain that
returns to a lower layer would close a cycle.

## Recorded debt

Some edges against the direction exist today (`ava` using `agent`, `gateway` using `cli`, tests
that reach up). They are not exemptions: each fact on such an edge is a recorded `cycle-edge` debt in
the manifest of the directory whose graph has the cycle, counted per source and target file, so a new
import on the same edge is a new violation and the recorded ones can only shrink. `python -m meizel debt`
shows what is left. See [recorded debt](../../meizel/docs/checker/debt.ava.okf.md).

## Packages outside the stack

- **`services`** is not a layer. The manifests fix four things: `base` does not use it; it does not
  use `cli`; it uses `agent` only through the narrowed hosted-runner edge; and nothing below `agent`
  uses `services.agent_runner.agent_host`. That puts `services` below `cli`, above `base` and outside the
  agent kernel. Nothing names a direction between `services` and `gateway`, `ava` or `ops`.
- **`ops`** is above `base` and below `gateway` and `cli`.
- **`ava_builtins`** (built-in plugins and skills) imports `base`, `ava`, `agent`, `ops` and `services`,
  and `agent` and `ops` each import back into it; those cycles are recorded debt.
- **`scripts/`, `tests/`, `schedules/` and `demos/`** are directories of the root like any other: what
  they import is declared in the root manifest, and their own tests under a package are judged in that package.

## What the check sees

- Every `import` in first-party Python, function-local ones included, and the other dependency
  forms the checker derives (fixture requests, object-form patches, declared loads). A string-form
  `importlib.import_module(...)` is a restricted form, not an invisible one.
- Test files, like everything else: a package's own `tests/` directory is a child of the package and
  its edges are declared like any other.
- A name with a leading underscore is private to its file, and a directory's `exports` say which
  children may be entered from outside.

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

These facts are separate from meizel's: they serve test selection and the tests-location proof, not the layering.
CI reverse impact follows unpruned runtime dependencies; test ownership remains
a separate query with its existing legacy subject policy. In particular, fixture
execution dependencies do not automatically define a test's business owner.
The pure declarative reader in `imports/fixture_scopes.py` is shared by the
pytest plugin and CI; their execution and selection policies remain separate.

## When an import is refused

A primitive that both sides need moves down to the lowest layer both may import, usually `base`,
and both import it from there (a data-plane path, a listener probe or a URL validator that a daemon
and `cli` both need lives in `base`). Recording the edge as debt is for an existing violation that
cannot be fixed in this change, never the way out of a new one; see the
[contributing guide](../contributing.md#locality-gate-meizel).
