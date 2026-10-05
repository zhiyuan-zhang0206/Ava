# Graph-edge hooks: function → class (`Hook` base, override `__call__`)

**Decision.** A graph-edge hook is no longer a bare `async def (state, runtime,
config) -> dict | None` registered by a decorator. It is a subclass of a new
`Hook` base class (`agent/hooks/_registry.py`) that overrides a typed
`__call__`; a plugin registers an **instance**:

```python
class WatchTokenCount(Hook):
    async def __call__(self, state, runtime, config, /) -> dict | None:
        ...

register_before_llm(WatchTokenCount())
```

PyTorch-`nn.Module` shape: the base pins the call signature, the subclass supplies
the body, and an instance can carry per-hook state/config on `self` instead of in
a module global.

## Why a class at all

The hook contract was previously a `typing.Protocol` (structural) that a bare
function satisfied. That works, but the signature is only *described*, never
*owned*: a plugin author who gets the parameters wrong finds out at runtime (or
not at all — a hook that quietly reads the wrong field just misbehaves). Moving to
a base class makes the signature an inherited, checked contract:

- **pyright enforces the override.** Under strict mode `reportIncompatibleMethodOverride`
  is an error, so a subclass whose `__call__` narrows a parameter, widens the
  return, or drops an argument fails the type check. The signature contract is now
  static, not conventional. `tests/agent/hook_typing_contract.py` proves this is
  live (see below).
- **Instances carry state.** Registration is an instance, so a hook that needs a
  threshold / counter / handle keeps it on `self` — no module-level singleton
  bookkeeping. (The built-ins are stateless, but the door is open.)
- **Call sites are unchanged.** An instance is callable, so the runner still does
  `hook(state, runtime, config)` and every test that called a hook directly still
  does. The migration bound each hook's existing public name to an instance of its
  new class, so `_repair_dangling_tool_use`, `syntax_fix_before_exec`,
  `_loaded.sdk_nudge_after_exec`, etc. keep resolving — now to a callable instance.

## Alternatives considered

- **Keep `Protocol` + a `TypedDict`-shaped signature.** Rejected: a Protocol is
  structural, so nothing forces a plugin's function through it — pyright only
  checks conformance at an explicit annotation site, and hooks are registered by
  value. A base class checks every subclass unconditionally.
- **`ABC` (nominal) vs `Protocol` (structural).** Chose `ABC`. We *want* the
  nominal relationship: "a hook IS-A `Hook`", registered by subtype, instantiation
  guarded by the abstract method (a subclass that forgets `__call__` can't be
  constructed — a runtime contract `tests/agent/test_hook_base.py` covers). A
  Protocol gives neither the abstract-method guard nor a single registration type.
- **Per-hook-point generics (`BeforeLlmHook` / `BeforeExecHook` / `AfterExecHook`).**
  Rejected. All three points share the *identical* signature `(state, runtime,
  config) -> dict | None` and the identical return contract (dict → reducer update,
  `{"goto": …}` → routing override); the only difference is which list an instance
  registers into, which the three `register_*` functions already express. Splitting
  into three near-empty subclasses would add surface for a distinction the types
  don't actually make. If a hook point ever grows a genuinely different shape,
  *that* is when a per-point generic earns its place.
- **Keep `__call__` as the override target vs a separate `forward`.** PyTorch
  splits `__call__` (bookkeeping) from `forward` (user body) so the base can wrap
  every call. Our runner already does the wrapping (node lifecycle, co-write
  detection) one level up, so there is nothing for a base `__call__` to add —
  subclasses override `__call__` directly.

## The static contract is self-guarding

`tests/agent/hook_typing_contract.py` is a pyright-only module (not collected by
pytest). It defines one correct override (`_Ok`, no suppression) and three
deliberately-wrong ones (`_BadParamType`, `_BadReturn`, `_BadArity`), each carrying
a `# pyright: ignore[reportIncompatibleMethodOverride]` on its `__call__` line. The
file header turns on `reportUnnecessaryTypeIgnoreComment=true`, which makes those
ignores **load-bearing**: if the base ever stops pinning the signature, the
override errors vanish, the ignores become unnecessary, and pyright fails on this
file. So a regression in the contract turns CI red here — and a bad plugin override
turns red at its own definition. Scoped via a file-level comment so no global
pyright setting changes.

## Migration was behavior-preserving by construction

Every existing hook body was moved verbatim into its subclass's `__call__` (the
transform re-indented the body and protected multi-line string interiors). The
migrated bodies are **statement-for-statement AST-identical** to the originals for
all nine hooks (two built-in: compact, repair; six plugin; one demo). No
double-track: the decorator/function registration form is gone, and `register_*`
now types its argument as `Hook`, so passing a bare function or a class (rather than
an instance) is a type error.
