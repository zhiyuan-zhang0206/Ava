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
`[type-checking.files]` in `meizel.policy.toml` with a reason. This rule is lint-enforced by
`scripts/lint/code_structure.py`.

## Per-file line budget: 800 lines

A `.py` file may contain at most 800 lines (`len(text.splitlines())`). Split
larger files into focused modules. The budget covers the eight governed
packages (`agent`, `ava`, `ava_builtins`, `gateway`, `base`, `services`, `ops`,
`cli`) plus `tests/` and `scripts/`. The TYPE_CHECKING ban and retired ambient machine_role()
call rejection apply only to the eight packages.

A script shipped with a skill or plugin gets no exemption. When one outgrows
the budget, its logic moves into a governed package the script imports
normally (the Claude/Codex launchers live in `ava/shell/coding_tools/`, their
`spawn_*.py` scripts only parse arguments). Splitting it into path-imported
siblings is refused by Rule 6 below. Moving implementation into a governed
package does not make it a public SDK capability; see [SDK and skill ownership](#sdk-and-skill-ownership).

Every over-limit file fails the gate. File-budget baselines have been removed
and cannot be reintroduced. Enforced by `scripts/lint/code_structure.py`.

## Directory budget: ≤20 direct entries

Every directory except the repository root may have at most 20 direct
Git-tracked entries. All files count regardless of suffix or language: Python,
TypeScript, Markdown, SQL, binary assets and other tracked files use one slot.
Hidden entries, `docs/`, `tests/`, migrations, symlinks and gitlinks also count.
A subdirectory takes one slot in its parent and has its own independent limit;
nesting depth is unrestricted. Symlink targets, gitlink contents and untracked
local artifacts are not traversed or counted.

Directory-budget baselines and per-directory exemptions are not allowed. A full
gate run checks the entire tracked tree. An explicit directory target checks
itself, its tracked descendants and its ancestors; a file target checks every
ancestor directory. `--only` accepts every tracked file type, including dangling
symlinks. Git query failures fail the gate rather than weakening the count.
The remaining site-exemption guard runs in both modes. Python file, function and
AST rules keep their existing scope; the universal directory limit does not
apply those rules to documentation or frontend source.

Enforced by the existing `scripts/lint/code_structure.py` and its pre-commit hook.

## Locality: file privacy, component entries and single owners

A `_`-prefixed member or module is private to its definition file, tests included.
Moving a test or a module does not grant private access, and removing an underscore
or adding a forwarding export is not an interface design. Standard Python language
protocols are told apart by exact names; arbitrary double-underscore library
metadata is not exempt.

Dependencies between files and directories are judged by meizel
(`python -m meizel check`): a directory's `meizel.toml` declares which of its children may
use which, and a name another file uses is public. Reaching a private name or module is
`private-name` / `private-module`; entering a directory through a child it does not export is
`not-exported`; a dependency nobody declared is `undeclared-edge`; patching anything is a
`*-patch` diagnostic. Existing violations are recorded debt that may only shrink. See
[the checker](../../meizel/docs/checker/checker.ava.okf.md) and the
[contributing guide](../contributing.md#locality-gate-meizel). Use an existing capability,
pass the required dependency explicitly, or verify public behavior. Agent visibility remains owned
by the existing SDK surface metadata and Installation; do not introduce another SDK member list.

- **Rule 6 — no path imports under `ava_builtins/`.** A skill or plugin
  module may not edit `sys.path`, call `site.addsitedir`, or load a module
  by file path (`spec_from_file_location`, `SourceFileLoader`,
  `runpy.run_path`); `meizel/language/path_imports.py` finds them. Shared
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
- **Rule 5 — single decision owners.** `meizel/owners/decisions.py:DECISIONS`
  names design decisions with exactly one owning module — today,
  `postgres-dial` (`base/db/connections.py`). Any other module making that
  decision is a bypass; fix it by routing through the owner. A site that
  genuinely cannot is listed under `[single-owner.<decision>]` in `meizel.policy.toml` with a one-line
  reason — an allowed module that stops bypassing (or disappears) fails as
  stale, so the map cannot rot into a permission wall. Add a new single-owner
  decision only once its owner exists: an entry in `DECISIONS` with its owning
  module(s), a `find(tree, roots)` AST scanner, and a `fix` message.

Rules 5 and 6 reject every measured site directly. The retired
`private_imports`, `owner_bypasses`, `path_imports` and `patch_targets` baseline fields may not
be reintroduced, even empty, in the working tree or in the comparison revision.

Only ambient-state sites retain exact `path::target -> site count` maps in their
baseline shards until the remaining debt is cleared. Their guards remain shrink-only,
including when a rule version changes; see the ambient-state lint owner.

A test in the top-level `tests/` has no baseline section to be frozen in. Complete
subject evidence with a repository-root LCA needs no filename registration;
unknown inputs prevent that proof. Existing by-design and historical
`scripts/structure/tests_location_allowed.py` policies remain during migration.
Other root tests are refused by `scripts/structure/tests_location.py`.

What this means for common edits:

- **Splitting a file, or moving code to another file**, cannot carry a frozen
  site to the new file — fix the reach-in or bypass as part of the split.
- **Moving a module into a subpackage** can change its component boundary;
  migrate consumers through the actual owner contract. Private members remain
  file-local before and after the move.
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
per-iteration `async with asyncio.TaskGroup()` for bounded parallelism. An explicit
Task requires a service owner that retains its identity, collects completion and
errors, and provides finite stop/join observation and honest unfinished results.
A Thread
requires an explicit service/process owner with admission, stop, retained handles
and completion/error collection; free-floating `create_task` or thread work is
forbidden. Rule 9 of `scripts/lint/code_structure.py`
(`meizel/state/ambient_state/`) enforces both: new sites fail, today's are
frozen in the `ambient_state` baseline section as `path::rule:name -> site count`
(exact, shrink-only), and the only things let through are the closed, reasoned lists
in the `[ambient-state]` tables of `meizel.policy.toml` — write-only facades, framework wiring
(`FastAPI()`, routers, argparse/Typer, `StateGraph`, locks), pure constants
(`re.compile`, `TypeVar`, `timedelta`, `Path`) and memoized pure functions. There is
no inline exemption; `schedules/` is in scope, tests, `__main__.py` and skill scripts
are not.

Thread and Task evidence share executable method syntax through the actual
`ambient_state/owner_method.py` definition owner. It supplies direct instance
fields, visible assignments/calls and join timeout syntax, excluding nested
uninvoked definitions and constant-dead branches. Each proof owner retains its
lifecycle decisions; dynamic timeout syntax does not prove a finite runtime bound.

Task ownership detection recognizes necessary same-instance wiring, with no file
list, fixed owner names or marker. Each explicit spawn assigns its actual Task and
immediately calls a synchronous registration method, before any suspension. That
method really inserts the same Task into an owner-created set/dict and attaches a
synchronous completion callback to it. Registration cannot yield, return early,
replace the Task or immediately remove it. The callback reads that Task's
exception, retains the original error in an owner field/list, and passes it to an
observer, directly or through one synchronous same-class helper. The owner's
async teardown awaits `asyncio.wait` on the same registry or its direct snapshot,
with an explicit non-None timeout; actual synchronous helper edges cancel work
or request stop through an owner-created Event/Future consumed by its registered
worker. An actual retained Popen also permits EOF on its stdin when the same
worker awaits a bounded wait on that process. These are structural request-stop
edges, not proof of the native EOF protocol or deadline. Teardown raises from
the same error receipt and provides unfinished Task identities/names
from that registry. Scalar task slots, async registration/callbacks, deeper helper chains
and unsupported aliases remain findings. TaskGroups remain available for work
whose lifetime and failure impact fit their scope; changing a raw spawner's
receiver name does not establish ownership.

A Task-valued map can also register the original Task directly, followed
immediately by its actual completion callback. Stop must cancel that map's
values, await their exact snapshot with a bounded timeout (directly or through
one awaited same-class helper), and preserve unfinished slots exposed by a
synchronous lookup. Map keys, replaced snapshots and decorative stop/admission
gates do not establish these facts.

These facts do not prove correct claimed/abandoned routing, retirement timing,
admission after stop, generation identity, error classification or every control
flow path. Reading `Task.exception()` leaves the Task result available to the
active caller; it does not authorize retiring an unclaimed result. Tests must
prove those handoffs and late-error visibility, preserve original error identity
and primary failures, and release/join their blocked work. Finite owner observation
does not promise Task termination or finite `asyncio.run` shutdown. Ordinary
best-effort telemetry/Redis may return honest unfinished work under the accepted
decision; business data and durable audit have no such relaxation.

Thread ownership detection is conservative and structural, with no file list or
inline owner marker. The scanner recognizes an instance-held Thread whose target
is a visible method, a unique constructor/start in the initializer, a real Event created by that owner and read
or passed to the worker, and a teardown call graph that sets that same signal,
joins that same handle with an explicit non-None timeout, checks its liveness,
and raises the original error retained by the worker. The worker entry captures
BaseException into an owner field and immediately passes it to an error observer.
Its else block contains no unprotected work, and any finally block only sets
owner-created completion Events. Repeatable start methods remain findings;
a stop-only fence does not prevent overwriting an active thread's handle.
Void completion is observed through join and liveness; value-producing work also
needs its actual result receipt. Method and field names carry no exemption.
Other ownership shapes remain findings until their structural evidence is
supported; they must not be disguised behind Timer, executor, helper or subclass.

AST does not prove that the composition root calls teardown, that branch ordering
is safe, that a dynamic timeout is finite, that a callback observes an Event, or
that an observer records a visible failure. It also cannot prove correct result
semantics, cross-generation receipt retention, preservation of a primary error,
or interruption of native blocking work. Consumer tests and review must cover
these properties, including admission after stop, late errors, finite shutdown
and residual resources. A saved handle, a log call or a syntactic join alone is
insufficient. See the [accepted ownership and shutdown choices](../decisions/engineering/design/simplification/2026-10-10-owned-workers-and-bounded-shutdown.md).

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

The remaining site-exemption guard selects one comparison commit:

1. If `LINT_STRUCTURE_BASELINE_BASE` is set, resolve that exact value to a commit,
   including in a shallow checkout. An empty or unresolvable value is a hard error.
2. Otherwise use the merge base of `HEAD` and `origin/main`. A missing ref or failed
   merge-base query is a hard error. A local checkout or fork without `origin/main`
   can explicitly set `LINT_STRUCTURE_BASELINE_BASE=HEAD` or another fetched revision.

Git rename-query and historical-read failures are hard errors. The guard reads the
baseline at the selected commit; a revision without the baseline directory, with a
retired section (even an empty one) or with a malformed baseline fails.
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

Unexpected failures must reach the operation or service owner. Configuration,
schema, invariant and programming errors must not become defaults, stale values,
skipped components or successful results. Logging an error does not authorize
recovery.

Retry or fallback requires an explicit contract for the expected failure: name
the exception types or protocol statuses, the recovery behavior, its budget and
the result when that budget is exhausted. Network timeouts, connection loss,
HTTP 429 and selected temporary server failures can qualify when the owning
operation designs for them. Invalid configuration or malformed data cannot be
classified as transient merely because it arrived through a network call.
Tests must exercise both the expected recovery and propagation of an unexpected
failure. Declare an optional capability's absence directly; a missing required
dependency is an error.

A broad handler (`except Exception`, `except BaseException`, bare `except`,
`contextlib.suppress(Exception)`) is not a recovery policy. Use it to clean up
and re-raise, or at a terminal boundary to report an explicit failed operation.
Do not keep a loop alive or replace a failed read just because the exception was
reported. Preserve cancellation and the original failure during cleanup. A
failure report needs a traceback at WARNING or above
(`logger.opt(exception=True).warning(...)`) or an explicit structured failure;
a debug or info line is not a report.

`scripts/lint/diagnostics/no_silent_failures.py` (hook `lint-no-silent-failures`)
checks the minimum reporting requirement, not whether recovery is legitimate.
Passing it does not approve a fallback. Its existing `# silent-ok: <reason>`
marker is restricted to a reporting channel's own failure path and does not
permit recovery in the operation that channel reports on.

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
adding a public API. A script may reuse existing non-agent-facing component capabilities through
an explicit owner interface without turning its selected output or procedure
into a new SDK method. If size limits or shared implementation require a governed module,
keep the workflow entry in the skill and the helper internal; placement under
`ava/` alone does not justify adding it to `__all_for_ava__`.

## SDK return shapes

A public SDK function or constant that returns a record (a fixed set of
meaningful fields) returns a `NamedTuple`, never a bare `tuple[A, B]` whose
meaning depends on position. The type must be listed in `__all_for_ava__` of the
namespace that returns it, so `help()` renders its fields; document each field in
the class docstring. Use `tuple[T, ...]` or a list for a variable-length
sequence of one type, and a frozen dataclass when the record has many fields,
optional fields or is expected to change. A constant of a `NamedTuple` type
cannot be wrapped with `ava.const()`; document it with a PEP 224 docstring.

## When a public SDK entry earns its place

A public SDK function must meet at least one of two tests:

1. It guards an invariant the underlying API cannot: a cross-process or gateway
   protocol, authorization, an atomic multi-step operation, lifecycle, or
   framework-internal state.
2. It makes a frequent operation shorter or harder to misuse. The typical case
   is a narrow signature: `ava.tasks.log` takes only a progress note, so an
   agent cannot resend an already-written result through `update`;
   `ava.tasks.create_and_assign` joins creating a task and assigning it into
   one step.

A function that meets neither and only renames another entry is deleted. A rule
that can sit on the underlying API belongs there (the `skill_invoked`
attribution rides on `files.read`), not behind a second entry point. Parameters
used only by the framework stay out of the agent-visible signature; give the
framework its own internal function.

## Import layering

Which package may import which, and how it is enforced:
[`import-layering.md`](import-layering.md).

## contextvars are allowlisted, not free

Use LangGraph's existing `Runtime[AvaContext]` for graph-run dependencies rather
than creating a second implicit context with `ContextVar`. Nodes receive the
runtime explicitly; helpers receive the values they need as parameters. Outside
a graph run, use the caller's explicit context or resource owner: `get_runtime()`
requires an active runnable context and is not a process-wide service locator.

The disposable execution child has no LangGraph runtime. Its bootstrap initializes
`ava.context`, `ava.state` and `ava.state_update` before user code runs; normal
imports and threads in that one execution use those same child-local slots.
SDK modules read context through `ava.context`, not a separate context variable,
thread inheritance patch or hidden host-current getter. Shared-host code receives
`Runtime[AvaContext]`, an explicit context or the narrower dependency it needs.
Plugin context-note builders receive `AvaContext` explicitly. See the
[process-local SDK decision](../decisions/agents/context/2026-10-09-process-local-sdk-context.md).


`contextvars` imports are banned by ruff `TID251` except in the mechanism
files on the allowlist (`pyproject.toml` — `flake8-tidy-imports.banned-api`
plus the `per-file-ignores` entries). LangGraph's runtime itself propagates
contextvars (pregel `copy_context`, `get_runtime`), and native metadata /
telemetry readers sit outside node signatures, so a blanket
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

The gap is invisible for a well-behaved child and load-bearing when the
spawned process is a launcher for the real work (git, ssh): orphaned `git` +
`ssh` + `sh` processes accumulated below a killed launcher. Anything with a
shell in the middle (`shell=True`, a `-lc` wrapper) has the same shape.

Git specifically: pass `env=base.host.git.gitenv.git_env()` so a credential prompt
errors instead of blocking on a terminal that does not exist, and ssh neither
asks nor dials unbounded. Note that `ConnectTimeout` is not the bound — an
`ssh` process can reach a state where its own timeout never fires, so the
caller's bound is the only real one.

Not lint-enforced repo-wide yet; the modules that drive git are guarded by
`base/host/tests/test_proc.py::test_git_driving_modules_do_not_bound_with_subprocess_run`.

## Tests do not patch; production takes what a test needs to control

A test does not replace a name in another module (`monkeypatch.setattr`,
`mock.patch`, `patch.object`, `patch.dict`, `monkeypatch.setenv`). When a test has to
control something, the production code takes it as a parameter, a constructor argument
for a class or a required keyword for a function, and the test passes a fake or a
value. Every patch is recorded meizel debt that may only shrink
([patching](../../meizel/docs/checker/patching.ava.okf.md)), and a new one fails the
check.

Why:

- **A patch finds its target by name, and a wrong name fails silently.** A patch aimed
  at a name that moved, was re-exported, or was bound into another module by
  `from x import y` still passes while the test stops testing what it claims. Splitting
  one `ops` module cost 81 `setattr` repoints across 6 test files over 12 names.
- **The dependency is visible where it is used.** A reader sees from the signature what
  a function decides on, without opening its tests to learn which names they replace.
- **A patch is process-wide.** `monkeypatch.setattr(mod.time, "monotonic", f)` rewrites
  the shared `time` module, not an attribute of `mod`: asyncio's `loop.time()`, other
  threads and every library in the process read `f` too.
- **A parameter follows a refactor.** It moves with the function. A patch target is a
  coordinate, and the next move breaks it.

What tests need sets the scope. Inject a time source, a process runner or a path where
a test has to control it; production code that no test needs to steer may still
`import time`. There is no blanket ban, and no static analysis that sorts a call into
"decision" or "record".

### The one allowed form per kind of dependency

| The test needs to control | The only form |
|---|---|
| Configuration, including a value that comes from an environment variable | The component takes its own frozen `XxxConfig`, with exactly the fields it reads. The composition root builds it from `settings`; the component never reads `settings` or `os.environ`. A test builds the config it wants. |
| A tuning constant (timeout, threshold, interval) | A field of that config whose default is today's constant. A test passes a small value. |
| A client, a factory | A required parameter with no default. |
| A path | A parameter. A test hands in `tmp_path`; it does not switch `AVA_HOME`. |
| Time: elapsed time, waiting, the wall clock | A required `Clock` (wall time and timezone; it carries a `Timers` and satisfies it) or, for a component that needs only elapsed time, a required `Timers` (`monotonic`, `sleep`, `asleep`). A root builds `Clock(config, timers=SYSTEM_TIMERS)` or passes `SYSTEM_TIMERS`; a test passes `ManualTimers` from `base/clock/tests` (`Clock(config, timers=manual, now=manual.now)`), which never waits and records each sleep in `sleeps`. A test never patches `time`, `asyncio.sleep` or `datetime`. |
| A socket or a server | A real local service: a listener on port 0, a unix socket under `tmp_path`. Only fault injection (a refused or half-open connect) goes through a `connect` parameter, and only in the clients of `services/redis_bridge` and `base/host/permissions_helper`. |
| A logger, a meter, a tracer | Production may use the global facade, because it is write-only and decides nothing. A test does not patch it; it reads what was recorded through the capture fixture the facade's owner exports from its `tests/`. |
| Processes, programs, the platform | A required `Host` (`base/host/system/hostport.py`): `run` (`proc.run_bounded`), `which`, `platform` and `uid`, and nothing else. A root builds `Host.system()`; a test builds the `Host` it wants, with `FakeCommands` from `base/host/tests` as `run` (it records argv, answers by prefix and fails on an unscripted command) and plain values for the rest. A test that needs a real process uses `Host.system()`. A test never patches `subprocess`, `shutil.which`, `os.access`, `sys.platform` or `platform.system()`. |

A fake lives in the `tests/` of the component that defines the port and is exported by
that directory's `meizel.toml`; a test of the component or of a layer above it imports
it from there.

### Composition roots and required dependencies

A composition root is where a process builds its parts: a process entry point, a CLI
command's `CommandRuntime.create()`, or a component's wiring module. It is the only
code that reads `settings`, the environment or `.env`, and it constructs configs,
clients and the system implementations of ports, then passes them down as arguments.
That `settings` is read only there is a convention that reviewers hold, not a lint
(decided deliberately): a component that imports `settings` is a defect even though no
check reports it.

A dependency is a required parameter. A default (`client: Client = Client()`), a
module-level instance, or a function that builds its own client when none is passed puts
the dependency back out of sight, and a module entry that calls a factory itself has
become a second composition root. Defaults are for tuning constants only.

### Existing patches

Existing patches stay recorded debt until their component is converted. Do not add one,
and do not repoint one at another module to keep a test green. When a change makes a
patched thing injectable, convert the tests of that component in the same change.
`stub_everywhere` in `tests/fixtures/guards.py` reaches aliases that are already frozen;
it serves the debt that remains and is not a pattern for a new test.

## A function-local `import base.x` makes `base` local to the whole function

A function that reaches a module as `base.deploy.state.host_deploy_state.read()` through
a module-level `import base.deploy` breaks if the same function also contains
`import base.db`. The statement binds `base` as a *local* for the entire body, because
the binding is decided statically, so a module-level import does not rescue it, and
every `base.…` above that line raises `UnboundLocalError`:

```python
import base.paths          # module scope: irrelevant to the function below

def is_paused():
    state = base.deploy.state.host_deploy_state.read()     # UnboundLocalError
    import base.db                            # <- makes `base` local for the whole body
```

It fails at runtime, on that branch only, and ruff does not report it. Pyright does
(`"base" is unbound`), so run it on the file you edit rather than waiting for a test to
reach the line. Hoist the import to module level. A function-local `from base.x import y`
is safe, since it binds `y`, not `base`.

## Role-scope check for per-machine surfaces

Before merging a new or changed endpoint that returns or fans out per-machine
data, classify the surface and scope it:

- **role-neutral** — applies to every host; no filter.
- **agent-runner-only** — filter `'agent-runner' = ANY(role)`, guard host-side op.
- **gateway-only** — guard `'gateway' in machine.role()` using the explicit root-owned `MachineIdentity`.

The roster (`/api/cluster/roster`) legitimately lists every host — it shows
`role` as a column, not agent-runner-only data.

## Lint vs Sweeper boundary

Which debt is a blocking lint here vs a periodic Sweeper finding is decided by
the graduation test in [`lint-vs-sweeper.md`](engineering/lint-vs-sweeper.md).
