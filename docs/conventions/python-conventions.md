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
`scripts/lint/code_structure.py`.

## Per-file line budget: 800 lines

A `.py` file may contain at most 800 lines (`len(text.splitlines())`). Split
larger files into focused modules. The budget covers the eight governed
packages (`agent`, `ava`, `ava_builtins`, `gateway`, `base`, `services`, `ops`,
`cli`) plus `tests/` and `scripts/`. The TYPE_CHECKING ban and machine_role()
allowlist still apply only to the eight packages.

A script shipped with a skill or plugin gets no exemption. When one outgrows
the budget, its logic moves into a governed package the script imports
normally (the Claude/Codex launchers live in `ava/shell/coding_tools/`, their
`spawn_*.py` scripts only parse arguments). Splitting it into path-imported
siblings is refused by Rule 6 below. Moving implementation into a governed
package does not make it a public SDK capability; see [SDK and skill ownership](#sdk-and-skill-ownership).

Every over-limit file fails the gate. File-budget baselines have been removed
and cannot be reintroduced. Enforced by `scripts/lint/code_structure.py`.

## Directory budget: ≤20 direct entries

Each directory in the same scope may have at most 20 direct entries:
`.py` and `.pyi` files plus direct subdirectories. A subdirectory counts as
one regardless of its contents — unless it holds nothing but `__pycache__` /
dot-prefixed entries (or nothing), the leftover a local package rename or
removal leaves and a fresh checkout never has, or it is a `docs/` or `tests/`
layer without `__init__.py` (such a package with `__init__.py` counts). A `tests/`
layer has no entry cap of its own either: a test is a file under any `tests/`
directory, the top-level one or a package's own `<pkg>/**/tests/`, and
`lint_common.is_test_path` is the one predicate every lint uses for it. Each
level is checked independently. `__pycache__`, dot-prefixed entries, and symlinks do not count
and are not traversed. `migrations` subtrees are entirely exempt. The repo-root `docs/` and
`ui/` are outside the scope.

Every over-limit directory in this scope fails the gate; directory-budget
baselines have been removed and cannot be reintroduced. A full gate run checks
the whole scope; an explicit directory target checks itself and its descendants,
and an explicit file target checks the file and its containing directory.
The remaining site-exemption guard runs in both modes.

## Locality: package doors and single owners

Two AST rules keep a change, or a reader tracing one, inside one package plus
its neighbors' public doors. The authoritative rule text — what counts as
private, what a bypass is, today's single-owner decision — lives in the
`scripts/lint/code_structure.py` module docstring (Rules 4 and 5); this
section covers fixing a violation; no locality baseline allowances remain. Rule 8 has
its own script (`scripts/lint/patch_targets.py`).

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
  exempt from the import and attribute check, but not from Rule 8.
- **Rule 8 — tests may not patch another package's private names.**
  `scripts/lint/patch_targets.py` rejects a patch (`monkeypatch.setattr`,
  `patch`, `patch.object`, `mocker.patch`, string or object target) of a
  `_private` name whose owning package does not contain the test's home. The
  home is the deepest package that holds or directly depends on everything the
  test imports (its own imports plus what the package imports), not its
  directory, so moving a test into `<pkg>/tests/` changes no verdict; the ambient environment
  (`base.config`, `base.paths`, machine identity, `AVA_*`) is exempt. Fix it
  by patching a public name, giving the owner an injection seam (a parameter,
  a settings field, a public setter), or moving the test into the owner.
- **Rule 6 — no path imports under `ava_builtins/`.** A skill or plugin
  module may not edit `sys.path`, call `site.addsitedir`, or load a module
  by file path (`spec_from_file_location`, `SourceFileLoader`,
  `runpy.run_path`); `scripts/structure/path_imports.py` finds them. Shared
  code moves into a governed package the script imports normally, and a
  script that needs it runs on the checkout's venv python. One narrow
  exception: a script under `ava_builtins/skills/<group>/<skill>/` may run a
  one-line `sys.path.insert(0, ...)` / `.append(...)` guard whose argument
  is derived from `__file__` and resolves inside that same `<skill>/` tree
  (its own `scripts/`, or a sibling sub-skill's) — recognized by AST, so it
  is not counted as a site at all. A guard reaching outside the skill (another
  skill, `ava_builtins/skills/` itself, or `ava_builtins/plugins/`), or any
  file-loader call regardless of its argument, is still a violation. The
  Every measured path-import site fails directly, with no baseline allowance.
- **Rule 5 — single decision owners.** `scripts/structure/locality.py:DECISIONS`
  names design decisions with exactly one owning module — today,
  `postgres-dial` (`base/db/connections.py`). Any other module making that
  decision is a bypass; fix it by routing through the owner. A site that
  genuinely cannot goes in that decision's `allowed` map with a one-line
  reason — an allowed module that stops bypassing (or disappears) fails as
  stale, so the map cannot rot into a permission wall. Add a new single-owner
  decision only once its owner exists: an entry in `DECISIONS` with its owning
  module(s), a `find(tree, roots)` AST scanner, and a `fix` message.

Rules 4, 5 and 6 reject every measured site directly. Their retired
`private_imports`, `owner_bypasses` and `path_imports` baseline fields may not
be reintroduced, even empty. Historical comparison revisions may carry empty
retired fields; nonempty historical fields are invalid too.

Rule 8 and ambient-state sites retain exact `path::target -> site count` maps
in their baseline shards until the remaining debt is cleared. Their guards
remain shrink-only, including when a rule version changes; see the patch-target
and ambient-state lint owners.

A test in the top-level `tests/` has no baseline section to be frozen in: it must stay by design
or be listed in `scripts/structure/tests_location_allowed.py` as `contract` or `integration`
with a reason, and any other one is refused (`scripts/structure/tests_location.py`).

What this means for common edits:

- **Splitting a file, or moving code to another file**, cannot carry a frozen
  site to the new file — fix the reach-in or bypass as part of the split.
- **Moving a module into a subpackage** narrows its owner: siblings that
  imported its privates become outside importers. Promote what they need, or
  keep them inside the new package.
- **A new CLI command** binds its `_h_*` handler directly in
  `cli/parsers/<domain>.py` (`set_defaults(func=_h_x)` referring to the
  function defined in that same module) — `cli.main` is not a handler
  registry; its tests patch the parser module before `build_parser()` runs.

## Ambient state: inject what is read to decide

Inject what is read to decide; write-only facades (a logger, a meter, a tracer) may
stay global, so for anything a module holds at import — an instance, a container,
a `global` slot, a `ContextVar`, a platform constant, an import-time call or read —
ask whether anything reads it to decide what to do, and if so build it in a
composition root and hand it to the component that uses it. Background work must be
durable or re-derivable from durable state and run as its own service loop; use a
per-iteration `async with asyncio.TaskGroup()` for bounded parallelism, never a
free-floating `create_task` or thread. Rule 9 of `scripts/lint/code_structure.py`
(`scripts/structure/ambient_state/`) enforces both: new sites fail, today's are
frozen in the `ambient_state` baseline section as `path::rule:name -> site count`
(exact, shrink-only), and the only things let through are the closed, reasoned lists
in `scripts/structure/ambient_state/allowlist.py` — write-only facades, framework wiring
(`FastAPI()`, routers, argparse/Typer, `StateGraph`, locks), pure constants
(`re.compile`, `TypeVar`, `timedelta`, `Path`) and memoized pure functions. There is
no inline exemption; `schedules/` is in scope, tests, `__main__.py` and skill scripts
are not.

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

Every hard function violation fails, including after a file or function rename.
Complexity and nesting baselines and rename allowances have been removed and
cannot be reintroduced. Refactor the implementation until it meets the budget.

The remaining site-exemption guard chooses its comparison base in this order:

1. If `LINT_STRUCTURE_BASELINE_BASE` is set, use its merge base with `HEAD`,
   or resolve the value directly to a commit if no merge base exists.
   An unresolvable explicit value is a hard error.
2. Otherwise use the merge base of `HEAD` and `origin/main` when available;
   a failed merge-base computation emits a note and falls through.
3. Otherwise use `HEAD`.

The guard reads the baseline at that revision. An absent baseline emits a
note and skips comparison. Empty retired budget sections in the comparison
revision are discarded; nonempty retired sections or malformed baselines fail.
Retired sections are always refused in the working tree, including empty ones.
Every remaining site section stays shrink-only when a lint is added or its rule
version changes; a new measurement does not authorize new exemptions. This
catches raises after committing them too:
before the structural hooks run, CI sets the explicit base to the base
revision of the triggering event — the same revision the checked-out merge
ref was built from. Pinning both sides to one event matters: a base branch
that shrinks while a run waits in the queue would otherwise read every
in-between shrink as a phantom raise (task #4597). The guard runs for full
and explicit-target scans alike, while quality checks only inspect the
selected scope.

Run `.venv/bin/python scripts/lint/code_structure.py` for the full gate.
Complexity warnings go to stderr as a total function/file count and up to
30 per-file counts, sorted by count descending then path ascending; remaining
files and functions are summarized in a `rest:` line. Add
`--complexity-warnings-full` anywhere in the arguments to print every file
count. Explicit targets restrict warnings too; no warned functions means
no warning output. Warnings alone never fail the gate.

## No `print()` in framework code

Framework code logs via `base.log.logger`. `print()` is banned in framework
code by ruff `T20`. Exempt: `cli/` (terminal output), `ava/` + `plugins/`
(agent-facing dump), `scripts/` (tooling).
One-off legitimate cases use inline `# noqa: T201` with a reason.

`base.log.logger` is loguru: a message takes `{}` fields
(`logger.warning("gate for {} raised: {}", name, exc)`), never printf `%s`,
which loguru leaves in the text while dropping the arguments. A stdlib
`logging.getLogger(...)` logger is the opposite: it keeps `%s`
(`_log.warning("gate for %s raised: %s", name, exc)`), and a `{}` field with
positional arguments raises `TypeError` at emit, losing the line. Both
directions are enforced by `scripts/lint/diagnostics/loguru_format.py` (hook
`lint-loguru-format`), which also flags `exc_info=` on a loguru call — loguru
has no such parameter (the traceback rides `extra` and is lost), so it is
`logger.opt(exception=True)`; stdlib loggers keep `exc_info`.

## No silent failures

A broad handler (`except Exception`, `except BaseException`, bare `except`,
`contextlib.suppress(Exception)`) must re-raise or report; a swallowed failure is
indistinguishable from a feature that works. In order of preference: narrow the
handler to the exceptions the `try` body can legitimately raise (an expected
condition may then be handled quietly); delete it and let the failure surface;
keep the broad handler at a real boundary (a loop that must go on, best-effort
cleanup or telemetry) and log at WARNING with the traceback
(`logger.opt(exception=True).warning(...)`) or emit a structured event. A debug or
info line is not a report. Enforced by `scripts/lint/diagnostics/no_silent_failures.py` (hook
`lint-no-silent-failures`); the one exemption is `# silent-ok: <reason>` on the
handler line, for a reporting channel's own failure path.

## No decorative emoji in core Python

Agent + backend code stays glyph-free. Enforced by
`scripts/lint/diagnostics/no_emoji.py` (hook `lint-no-emoji`). Exempt: `cli/` and `ui/`
(deliberate-UX surfaces), prose/content (`skills/`, the doc axes, `ui/web/`).
Plain text marks (✓ ✗) are allowed. A line that genuinely needs the character
uses inline `# emoji-ok: <reason>`.

## SDK and skill ownership

The SDK owns reusable Ava domain operations and their execution contracts, such
as identity attachment, message delivery and file access. A skill owns how to
combine those operations for a particular workflow: preparation, context
selection, briefing assembly and handoff procedures belong in its instructions
or bundled scripts. One skill needing a convenient wrapper is not sufficient
reason to add a capability to the SDK.

Choose the owner from the operation's contract and actual consumers before
adding a public API. A script may reuse existing internal readers and runtime
primitives without turning its selected output or procedure into a new SDK
method. If size limits or shared implementation require a governed module,
keep the workflow entry in the skill and the helper internal; placement under
`ava/` alone does not justify adding it to `__all_for_ava__`.

## Import layering

Which package may import which, and how it is enforced:
[`import-layering.md`](import-layering.md).

## contextvars are allowlisted, not free

Use LangGraph's existing `Runtime[AvaContext]` for graph-run dependencies rather
than creating a second implicit context with `ContextVar`. Nodes receive the
runtime explicitly; helpers receive the values they need as parameters. Outside
a graph run, use the caller's explicit context or resource owner: `get_runtime()`
requires an active runnable context and is not a process-wide service locator.

`contextvars` imports are banned by ruff `TID251` except in the mechanism
files on the allowlist (`pyproject.toml` — `flake8-tidy-imports.banned-api`
plus the `per-file-ignores` entries). LangGraph's runtime itself propagates
contextvars (pregel `copy_context`, `get_runtime`), and the SDK / log /
telemetry / retry-policy readers sit outside node signatures, so a blanket
ban is not possible — but every use is a mechanism-layer decision. A new use
point needs a written justification in the PR description before joining the
allowlist. Each `ContextVar` is also a frozen `contextvar` site of the
[ambient-state rule](#ambient-state-inject-what-is-read-to-decide); the target is
the LangGraph runtime context (`AvaContext`), not a module global.

## Finite-domain vocabulary

Give a finite operational domain (status, outcome, policy or mode) one named
owner, normally a `StrEnum`. Producers, comparisons, transition tables and
consumers use that owner's members. Keep distinct domains separate even when
some serialized values coincide. Trace consumers before changing the vocabulary.

Convert raw wire, configuration and database values at their receiving boundary;
unknown values fail there. Preserve the boundary's existing error isolation.
Defaults apply to an explicitly optional input, not to an invalid value. Persisted
and wire strings are compatibility contracts; an enum refactor preserves them.

Schema discriminators and configuration choice annotations may require `Literal`.
Reference canonical members where that framework accepts them and verify the
consumer's supported set against the owner. When a protocol requires literal
discriminators, keep that declaration authoritative and derive its consumers.
Check DB constraints and generated schemas/types when their domains change.

Automation guards only the facts it can establish. The termination-source lint
rejects literal SQL or bind values outside its enum and bound `None` on terminated
writes. Dynamic SQL and runtime expressions need boundary validation and relevant
consumer tests. Domain ownership and whether a fallback is legitimate belong in
contribution review; a string search alone cannot decide them.

## Model new cross-process / cross-layer wire shapes

A payload crossing a process boundary (gateway↔agent-runner RPC, SSE events,
`additional_kwargs` metadata bags) gets a `BaseModel` / `TypedDict` / `StrEnum`
at the boundary, not a `dict[str, Any]` unpacked by hand at each call site.
`base/events/live/projection.py`'s discriminated union (`role: Literal[...]` discriminator +
a `TypeAdapter`) is the template. Not lint-enforced — see
the git log (typed-boundaries design record)
for why pyright's `reportUnknown*` family can't substitute for this.

## A subprocess timeout means `base.host.proc.run_bounded`

`subprocess.run(..., timeout=T)` bounds the process Python spawned, not the work
it started: on expiry Python kills that one process and every descendant keeps
running. Use `base.host.proc.run_bounded(argv, timeout=...)` instead — same shape,
but it kills the whole tree (descendants enumerated *before* the parent dies)
and still raises `TimeoutExpired`, so caller control flow is unchanged.

The gap is invisible on POSIX for a well-behaved child and load-bearing on
Windows, where `C:\Program Files\Git\cmd\git.exe` is a launcher stub for the real
git: the fleet's Windows agent-runner accumulated 66 orphaned `git.exe` + 66
`ssh.exe` + 63 `sh.exe`, all below a killed stub. Anything with a shell in the
middle (`shell=True`, a `-lc` wrapper) has the same shape on every platform.

Git specifically: pass `env=base.deploy.git.gitenv.git_env()` so a credential prompt
errors instead of blocking on a terminal that does not exist, and ssh neither
asks nor dials unbounded. Note that `ConnectTimeout` is not the bound — an
`ssh.exe` on that box reached a state where its own timeout never fired, so the
caller's bound is the only real one.

Not lint-enforced repo-wide yet; the modules that drive git are guarded by
`base/host/tests/test_proc.py::test_git_driving_modules_do_not_bound_with_subprocess_run`.

## Reach a stubbable name through its owning module

`from base.cluster import session_name` binds the function object into the
*reader's* module dict at import time, and that binding is what the reader
resolves. So the reader — not the owner — becomes the patch surface, and moving a
function to another module silently takes it out of reach of a patch aimed at its
old home. Splitting one `ops` module cost **81 `setattr` repoints across 6 test
files over 12 names** for exactly this reason; a re-export facade did not help,
because it fixes importers, not global resolution inside moved code.

So a name that a test would stub is **reached through the module that owns it**:

```python
import base.cluster
from ops.cluster.operations import pause as cluster_pause

base.cluster.session_name(service)      # not: session_name(...)
cluster_pause.unpause_local_cluster()     # not: unpause_local_cluster()
```

Which names: the state-touching ones — path resolvers, session liveness probes,
spawners, pause/unpause, anything that reads the filesystem, a subprocess or the
network. **Not** constants, exception classes, Pydantic models, type aliases, pure
formatters, or the `settings` singleton: nothing stubs them, so they carry no patch
surface, and `except cluster_rpc.ClusterOpUnreachable` only adds noise. A
function-local `from x import y` is already fine — it re-resolves per call, so it
reads the owner's current binding and survives its enclosing function moving.

**Before converting anything in a function, check that function for a
`import base.X` statement.** It binds `base` as a *local* for the entire function
body — the binding is decided statically, so a module-level `import base.paths`
does **not** rescue you — and every `base.…` above that line then raises
`UnboundLocalError`:

```python
import base.paths          # module scope — irrelevant to the function below

def is_paused():
    state = base.deploy.state.host_deploy_state.read()     # UnboundLocalError
    import base.db                            # <- makes `base` local for the whole body
```

Runtime only, on that branch only, and neither ruff nor pyright reports it.
`ops/cluster/pause.py` was exactly this shape, so its conversion had to hoist
`import base.db` to module level first. Hit blind, it reads as the whole approach
being unworkable rather than as one import in the wrong place. A function-local
`from base.x import y` is safe — it binds `y`, not `base`.

The trade is deliberate: source-patching has a **wider blast radius** than
patch-where-used. Measure it before arguing about it — a source patch only reaches
readers that *also* go through the module, so converting a name every other consumer
from-imports widens nothing today. Take the trade where the name is one fact per
process (there is one `$AVA_HOME`, one posture row, one session-naming
scheme — a second reader seeing the unpatched value is a bug, not precision).

Keep the from-import where the test's assertion is about *one call site's mechanism*
rather than about the value. A subprocess-boundary test must name the actual
call site when it proves that site's timeout, native custody or refusal behavior.
Patching the shared utility itself can also intercept unrelated consumers and
turn their work into an accidental fake.

Pre-existing aliases that cannot be converted away — a facade's own re-exports, and
consumers that from-import from it at module top level — are what
`tests/fixtures/guards.py`'s `_stub_everywhere` is for. The two mechanisms do not overlap:
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
the graduation test in [`lint-vs-sweeper.md`](engineering/lint-vs-sweeper.md).
