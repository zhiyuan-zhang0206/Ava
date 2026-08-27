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

## Per-file line budget: 500 soft / 800 hard

- ≤500 lines: normal.
- 500–800: transitional zone — tolerated, listed as a non-blocking nudge on a
  full run.
- >800: fails lint.

`_OVERSIZE_ALLOWED` grandfathers genuinely cohesive files (Settings aggregator,
SDK entry surface, schema block), plus a separately-marked section for files
inherited over-ceiling when a previously ungated package joined the scan — debt
with a pending split, not a blessing. Scope (`_SCAN_DIRS`) tracks
`[tool.importlinter] root_packages`, so a package declared a layer is gated;
`ava_builtins` is the one deliberate exclusion. Enforced by
`scripts/lint_code_structure.py`.

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

Git specifically: pass `env=shared.gitenv.git_env()` so a credential prompt
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
