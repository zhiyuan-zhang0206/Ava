# Python coding conventions

The mandatory coding rules enforced by pre-commit lints. Read when writing or
reviewing Python code.

## No `if TYPE_CHECKING:`

All imports go at top level — don't fold type-only imports under
`if TYPE_CHECKING:`. This repo is an application, not a library: every
dependency loads on the runtime path anyway, and frameworks like LangGraph /
Pydantic do runtime `get_type_hints()` / `inspect.signature` introspection
that would `NameError` on a TYPE_CHECKING-only import.

Genuine exceptions (circular imports, `import torch`-class heavy deps) go in
`_TYPE_CHECKING_ALLOWED` with a reason. This rule is lint-enforced by
`scripts/lint_code_structure.py`.

## Per-file line budget: 800 lines

A `.py` file may contain at most 800 lines (`len(text.splitlines())`). Split
larger files into focused modules. The budget covers the eight governed
packages (`agent`, `ava`, `ava_builtins`, `gateway`, `shared`, `services`, `ops`,
`cli`) plus `tests/` and `scripts/`. The TYPE_CHECKING ban and machine_role()
allowlist still apply only to the eight packages.

Existing over-limit files are frozen in `scripts/structure/baseline.json`.
New violations and growth above a frozen value fail the gate. The baseline is
shrink-only: a guard compares it with the base revision described below and
rejects added file entries or raised values. After splitting a file, lower
its baseline value by hand to its current line count, or remove its entry
once it is within budget. Enforced by `scripts/lint_code_structure.py`.

## Directory budget: ≤20 direct entries

Each directory in the same scope may have at most 20 direct entries:
`.py` and `.pyi` files plus direct subdirectories. A subdirectory counts as
one regardless of its contents; each level is checked independently.
`__pycache__`, dot-prefixed entries, and symlinks do not count and are not
traversed. `migrations` subtrees are entirely exempt. `docs/` and `ui/` are
outside the scope.

Existing over-limit directories are frozen in the `directories` object of
`scripts/structure/baseline.json`, with the same containment and shrink-only
rules as files. After reorganizing a directory, lower its count by hand or
remove its entry when it reaches the cap. A full gate run checks the whole
scope; an explicit directory target checks itself and its descendants, and
an explicit file target checks the file and its containing directory. The
baseline guard runs in both modes.

## Locality: package doors and single owners

Two AST rules keep a change, or a reader tracing one, inside one package plus
its neighbors' public doors. The authoritative rule text — what counts as
private, what a bypass is, today's single-owner decision — lives in the
`scripts/lint_code_structure.py` module docstring (Rules 4 and 5); this
section covers fixing a violation and maintaining its baseline.

- **Rule 4 — package doors.** Reaching a `_`-prefixed module or name from
  outside the package that owns it fails, whether by import or by attribute
  access on an imported module. Fix it either by using a public name through
  the owner's `__init__.py`, or by promoting the name into the owner's
  contract on purpose — export it / drop the underscore — so the widened
  contract shows up in the diff. There is no inline escape hatch and no
  per-site allowlist: a name another package genuinely needs is, by
  definition, part of that package's contract, so the fix is to make the
  contract honest rather than to excuse the reach-in. `ava` is no exception:
  what the agent sees is the `__all_for_ava__` whitelist
  ([SDK surface](sdk-docstring-discipline.md)), not the underscore, so an
  `ava/_*.py` module another package needs is promoted to a public module name
  without becoming agent-visible. Files under a `tests/` directory are
  exempt.
- **Rule 5 — single decision owners.** `scripts/structure/locality.py:DECISIONS`
  names design decisions with exactly one owning module — today,
  `postgres-dial` (`shared/db_connections.py`). Any other module making that
  decision is a bypass; fix it by routing through the owner. A site that
  genuinely cannot goes in that decision's `allowed` map with a one-line
  reason — an allowed module that stops bypassing (or disappears) fails as
  stale, so the map cannot rot into a permission wall. Add a new single-owner
  decision only once its owner exists: an entry in `DECISIONS` with its owning
  module(s), a `find(tree, roots)` AST scanner, and a `fix` message.

Both rules freeze today's sites in the `private_imports` / `owner_bypasses`
sections of `scripts/structure/baseline.json` as exact `path::target -> site
count` maps. Unlike the line/directory budgets, the count must match reality
exactly in both directions: a new or grown site fails, and a shrunk or removed
site fails too until its baseline entry is lowered or deleted — so a fixed
reach-in cannot silently return uncounted. Against the base revision both
sections are shrink-only: a new key is accepted only against a same-file
removal of the same private name with equal or greater value (the private
owner module moved), and a git `-M` rename carries keys once they are migrated
to the new path by hand.

What this means for common edits:

- **Splitting a file, or moving code to another file**, cannot carry a frozen
  site to the new file — fix the reach-in or bypass as part of the split.
- **Moving a module into a subpackage** narrows its owner: siblings that
  imported its privates become outside importers. Promote what they need, or
  keep them inside the new package.
- **A new CLI command** binds its `_h_*` handler directly in
  `cli/parsers/<domain>.py` instead of adding another re-export to
  `cli/main.py` (the existing re-exports are frozen debt); its tests patch the
  parser module before `build_parser()` runs.

## Function quality budgets: complexity and nesting

Every function and method in the same recursive `.py` scope has two budgets:

- **McCabe cyclomatic complexity (CC)** uses pinned `radon==6.0.1`.
  CC ≥15 is a hard violation; CC 10–14 is a non-blocking warning; CC ≤9
  is silent. Each file is parsed once. Methods in function-local classes
  omitted by Radon's whole-file collection are scored from their existing
  AST nodes with the same Radon calculation, so all functions remain covered.
- **Control-flow nesting** may be at most 5; depth >5 is a hard violation.
  Count `if`, `for`, `async for`, `while`, `try`, `with`, `async with`, and
  `match` along the deepest function-body path. An `elif` stays at its
  parent's depth (a sole `If` in `orelse` with the same column offset).
  A `try` adds one level to its body, handlers, `else`, and `finally`;
  handlers do not add a second level. Comprehensions, lambdas, decorators,
  and `with` items add nothing. Nested functions are measured separately;
  nested function and class bodies do not deepen the outer function.

Lambda expressions and class bodies themselves are not measured functions.
Keys use `<repo-relative .py path>::<qualname>` with Python qualification:
`name`, `Class.name`, `Outer.Inner.name`, or `parent.<locals>.child`.
Repeated qualified names in one file are ordered by source appearance:
the first keeps its key, then `#2`, `#3`, and so on are appended.

The `complexity` and `nesting` objects in `scripts/structure/baseline.json`
freeze hard violations, alongside `directories` and `files`. Function keys
must name scoped `.py` paths and non-empty qualified names. Values must be
integers at or above 15 for complexity, or above 5 for nesting; an entry
below its threshold is invalid. An unlisted hard violation or growth above
its frozen value fails. Existing violations at or below their frozen values
pass. Stale entries are allowed; delete them when the violation disappears.
Hand-edit entries only to lower a still-over-budget value or delete an entry.

Function renames have one allowance: an added function key must pair with a
distinct removed key in the **same section and file**, with a new value no
greater than the removed value. One removal cannot cover two additions.
File renames carry their frozen keys: when `git diff -M` detects a move between
the comparison base and the working tree (any similarity — a real move also
rewrites import paths, so keyed files typically land around R9x), the `files`,
`complexity`, and `nesting` keys of the old path are read as the new path's
keys. Migrate the baseline entries with the move — remove the old key, add the
new one with the same value; the guard accepts that edit. The frozen values
still cap the new path: a raised value or an unpaired new key stays a violation,
and a rewrite git no longer detects as a rename is evaluated fresh. Directory
counts follow the move (they are per-directory, not per-file). Directory and file sections never
permit added keys. All sections reject raised values. New files belong in existing subdirectories with room, or
arrive with a real directory split that lowers counts, without raising the
baseline.

The guard chooses its comparison base in this order:

1. If `LINT_STRUCTURE_BASELINE_BASE` is set, use its merge base with `HEAD`,
   or resolve the value directly to a commit if no merge base exists.
   An unresolvable explicit value is a hard error.
2. Otherwise use the merge base of `HEAD` and `origin/main` when available;
   a failed merge-base computation emits a note and falls through.
3. Otherwise use `HEAD`.

The guard reads the baseline at that revision. An absent baseline emits a
note and skips comparison; a legacy two-section baseline compares its file
and directory sections and notes that the new sections are introductions.
Malformed baselines fail. This catches raises after committing them too:
before the structural hooks run, CI sets the explicit base to the base
revision of the triggering event — the same revision the checked-out merge
ref was built from. Pinning both sides to one event matters: a base branch
that shrinks while a run waits in the queue would otherwise read every
in-between shrink as a phantom raise (task #4597). The guard runs for full
and explicit-target scans alike, while quality checks only inspect the
selected scope.

Run `.venv/bin/python scripts/lint_code_structure.py` for the full gate.
Complexity warnings go to stderr as a total function/file count and up to
30 per-file counts, sorted by count descending then path ascending; remaining
files and functions are summarized in a `rest:` line. Add
`--complexity-warnings-full` anywhere in the arguments to print every file
count. Explicit targets restrict warnings too; no warned functions means
no warning output. Warnings alone never fail the gate.

## No `print()` in framework code

Framework code logs via `shared.log.logger`. `print()` is banned in framework
code by ruff `T20`. Exempt: `cli/` (terminal output), `ava/` + `plugins/`
(agent-facing dump), `scripts/` (tooling).
One-off legitimate cases use inline `# noqa: T201` with a reason.

## No decorative emoji in core Python

Agent + backend code stays glyph-free. Enforced by
`scripts/lint_no_emoji.py` (hook `lint-no-emoji`). Exempt: `cli/` and `ui/`
(deliberate-UX surfaces), prose/content (`skills/`, the doc axes, `ui/web/`).
Plain text marks (✓ ✗) are allowed. A line that genuinely needs the character
uses inline `# emoji-ok: <reason>`.

## Import layering

`shared < ava < agent < gateway < cli` — a lower layer importing a
higher one fails; higher→lower is fine. `services` must not import the `agent`
kernel but is otherwise unlayered (it straddles). `plugins` is ungoverned
(agent ↔ plugins is cyclic by design).

Enforced by import-linter (config in `pyproject.toml [tool.importlinter]`,
hook `lint-imports`).

## contextvars are allowlisted, not free

`contextvars` imports are banned by ruff `TID251` except in the mechanism
files on the allowlist (`pyproject.toml` — `flake8-tidy-imports.banned-api`
plus the `per-file-ignores` entries). LangGraph's runtime itself propagates
contextvars (pregel `copy_context`, `get_runtime`), and the SDK / log /
telemetry / retry-policy readers sit outside node signatures, so a blanket
ban is not possible — but every use is a mechanism-layer decision. A new use
point needs a written justification in the PR description before joining the
allowlist.

## Model new cross-process / cross-layer wire shapes

A payload crossing a process boundary (gateway↔agent-runner RPC, SSE events,
`additional_kwargs` metadata bags) gets a `BaseModel` / `TypedDict` / `StrEnum`
at the boundary, not a `dict[str, Any]` unpacked by hand at each call site.
`shared/live_events.py`'s discriminated union (`role: Literal[...]` discriminator +
a `TypeAdapter`) is the template. Not lint-enforced — see
the git log (typed-boundaries design record)
for why pyright's `reportUnknown*` family can't substitute for this.

## A subprocess timeout means `shared.proc.run_bounded`

`subprocess.run(..., timeout=T)` bounds the process Python spawned, not the work
it started: on expiry Python kills that one process and every descendant keeps
running. Use `shared.proc.run_bounded(argv, timeout=...)` instead — same shape,
but it kills the whole tree (descendants enumerated *before* the parent dies)
and still raises `TimeoutExpired`, so caller control flow is unchanged.

The gap is invisible on POSIX for a well-behaved child and load-bearing on
Windows, where `C:\Program Files\Git\cmd\git.exe` is a launcher stub for the real
git: the fleet's Windows agent-runner accumulated 66 orphaned `git.exe` + 66
`ssh.exe` + 63 `sh.exe`, all below a killed stub. Anything with a shell in the
middle (`shell=True`, a `-lc` wrapper) has the same shape on every platform.

Git specifically: pass `env=shared.deploy.git.gitenv.git_env()` so a credential prompt
errors instead of blocking on a terminal that does not exist, and ssh neither
asks nor dials unbounded. Note that `ConnectTimeout` is not the bound — an
`ssh.exe` on that box reached a state where its own timeout never fired, so the
caller's bound is the only real one.

Not lint-enforced repo-wide yet; the modules that drive git are guarded by
`tests/shared/test_proc.py::test_git_driving_modules_do_not_bound_with_subprocess_run`.

## Reach a stubbable name through its owning module

`from shared.cluster import session_name` binds the function object into the
*reader's* module dict at import time, and that binding is what the reader
resolves. So the reader — not the owner — becomes the patch surface, and moving a
function to another module silently takes it out of reach of a patch aimed at its
old home. Splitting `ops/cluster.py` cost **81 `setattr` repoints across 6 test
files over 12 names** for exactly this reason; a re-export facade did not help,
because it fixes importers, not global resolution inside moved code.

So a name that a test would stub is **reached through the module that owns it**:

```python
import shared.cluster
from ops import cluster_session

shared.cluster.session_name(_UPDATER_SERVICE)      # not: session_name(...)
cluster_session._has_orchestration_session(updater_sess)    # not: _has_orchestration_session(...)
```

Which names: the state-touching ones — path resolvers, session liveness probes,
spawners, pause/unpause, anything that reads the filesystem, a subprocess or the
network. **Not** constants, exception classes, Pydantic models, type aliases, pure
formatters, or the `settings` singleton: nothing stubs them, so they carry no patch
surface, and `except cluster_session.OrchestrationSpawnFailed` only adds noise. A
function-local `from x import y` is already fine — it re-resolves per call, so it
reads the owner's current binding and survives its enclosing function moving.

**Before converting anything in a function, check that function for a
`import shared.X` statement.** It binds `shared` as a *local* for the entire function
body — the binding is decided statically, so a module-level `import shared.paths`
does **not** rescue you — and every `shared.…` above that line then raises
`UnboundLocalError`:

```python
import shared.paths          # module scope — irrelevant to the function below

def pause_local_cluster():
    state = shared.host_deploy_state.read()     # UnboundLocalError
    import shared.db                            # <- makes `shared` local for the whole body
```

Runtime only, on that branch only, and neither ruff nor pyright reports it.
`ops/cluster_pause.py` was exactly this shape, so its conversion had to hoist
`import shared.db` to module level first. Hit blind, it reads as the whole approach
being unworkable rather than as one import in the wrong place. A function-local
`from shared.x import y` is safe — it binds `y`, not `shared`.

The trade is deliberate: source-patching has a **wider blast radius** than
patch-where-used. Measure it before arguing about it — a source patch only reaches
readers that *also* go through the module, so converting a name every other consumer
from-imports widens nothing today. Take the trade where the name is one fact per
process (there is one `$AVA_HOME`, one posture row, one session-naming
scheme — a second reader seeing the unpatched value is a bug, not precision).

Keep the from-import where the test's assertion is about *one call site's mechanism*
rather than about the value. `shared.proc.run_bounded` stays from-imported into
`ops/cluster_deploy.py` on that ground: the test claims the validate-before-kill
fetch uses `run_bounded` rather than a plausible-looking `subprocess.run(timeout=)`,
so the stub has to name the site to mean anything. The widening there is also latent
rather than absent — `run_bounded` is the repo's universal subprocess primitive, so
the moment a second module reaches it through `shared.proc`, one test's source patch
starts faking that module's bounded work too.

Pre-existing aliases that cannot be converted away — a facade's own re-exports, and
consumers that from-import from it at module top level — are what
`tests/conftest.py`'s `_stub_everywhere` is for. The two mechanisms do not overlap:
this rule prevents new frozen aliases, that helper reaches the ones already frozen.

## Role-scope check for per-machine surfaces

Before merging a new or changed endpoint that returns or fans out per-machine
data, classify the surface and scope it:

- **role-neutral** — applies to every host; no filter.
- **agent-runner-only** — filter `'agent-runner' = ANY(role)`, guard host-side op.
- **gateway-only** — guard `is_gateway()` (`'gateway' in machine_role()`).

The roster (`/api/cluster/roster`) legitimately lists every host — it shows
`role` as a column, not agent-runner-only data.

## Lint vs Sweeper boundary

Which debt is a blocking lint here vs a periodic Sweeper finding is decided by
the graduation test in [`lint-vs-sweeper.md`](lint-vs-sweeper.md).
