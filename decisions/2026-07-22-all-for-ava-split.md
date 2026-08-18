# Split the agent-visible surface out of `__all__` into `__all_for_ava__`

## Context

`ava`'s per-module `__all__` had been quietly conscripted as the runtime
registry of the *agent-visible* SDK surface — the whitelist `ava.help()` renders
and the system prompt expands. That overloaded one name with two jobs that had
drifted apart:

- **It was not Python's `__all__` anymore.** The top-level list deliberately
  *excluded* Python-public members (`register_namespace`, `const`, `extend`,
  `state`, the exception classes) because they are plugin-author / framework
  API, not agent surface. Plugins mutated it at runtime (`register_namespace`
  appended, `register_namespace_member` appended to a parent's, teardown and
  `AVA_SDK_DISABLE` removed). `ava.self`'s `__all__` even listed
  `__getattr__`-served names (`MACHINE_SPEC`) that have no static binding, so
  pyright's `reportUnsupportedDunderAll` had to be suppressed with per-entry
  `# pyright: ignore`.
- **"Agent-visible surface" was implemented four times, inconsistently.** The
  help child walk, `_search_ava_for_function_binding`, the SDK-expand
  `_discover_all_namespaces`, and the metering `_instrument_targets` each
  re-derived it. The underscore guard that keeps a private name out of the
  agent's view was present in three and *missing in the help child walk* — a
  live leak: anything an `__all__` list happened to contain rendered verbatim.

## Decision

Give the agent-visible surface its own name, `__all_for_ava__`, and return
`__all__` to meaning only what Python means by it.

- Every `ava` namespace module (and the `ava.cwd` / `ava.tasks` plugin
  namespace modules, and `ava.mcps`'s dynamic `_ServerProxy`) declares
  `__all_for_ava__`. Plugin registration / disable / teardown mutate *that*,
  never `__all__`.
- One shared accessor, `ava.agent_visible_names(container)`, is the single
  source of truth. All four consumers route through it, so the underscore guard
  is written once — which closes the help-render leak.
- `__all__` is no longer defined on `ava` namespace modules at all (nothing does
  `from ava.X import *`; importability of re-exports is carried by
  redundant-alias imports — `from … import Y as Y` — which the type checker
  already honors). With the magic name gone from those modules, every
  `reportUnsupportedDunderAll` ignore disappears by construction. Framework
  layers that use `__all__` as a genuine Python re-export list (`shared/*`,
  `agent/*`, `cli/*`, `ops/*`, `gateway/*`) keep it untouched — they are never
  rendered by `ava.help()`, so there is nothing to split.

The migration is surface-preserving: `ava.help(ava)`, every `help(ava.X)`, and
the rendered SDK-expand section are byte-identical before and after (the only
behavioral change is the now-uniform underscore guard, which no current list
trips).

## Alternatives rejected

- **Keep `__all__` as a static twin** — declare `__all_for_ava__` *and* retain a
  hand-kept static `__all__` next to it on every `ava` namespace module, on the
  reading that "return `__all__` to its Python meaning" implies it must still be
  present. Rejected — this is the load-bearing fork, so the reasoning is spelled
  out to stop a future contributor from re-adding it:
  - **No consumer.** Nothing in the tree does `from ava.X import *`, and the
    cross-package re-exports that `ava/__init__.py` performs already carry their
    type-checker re-export marker via redundant-alias imports (`from … import Y
    as Y`), not via `__all__`. A retained `__all__` on these modules would be a
    list no code or tool reads.
  - **It would be a drifting duplicate.** For most modules the twin equals
    `__all_for_ava__` verbatim, so it is a second copy that silently rots the
    first time someone edits one and not the other. Where it would *not* be a
    copy — `ava.self`, whose agent surface includes `__getattr__`-served names
    (`MACHINE_SPEC`) — a valid static `__all__` would have to be a *different,
    trimmed* list to stay pyright-clean, i.e. more surface to maintain, not less.
  - **Absence is the honest static state.** A normal Python module that neither
    star-exports nor re-exports simply omits `__all__`; that omission *is* its
    correct static form. "Stop conscripting `__all__`" is satisfied by removing
    the conscripted list, not by leaving an inert placeholder. Deleting the magic
    name is also what makes every `reportUnsupportedDunderAll` ignore vanish by
    construction — a retained (even trimmed) `__all__` reopens that surface.
- **A — patch in place, keep the overload.** Add the missing underscore guard
  to the help walk, leave `__all__` doing double duty, keep the pyright
  ignores. Rejected: it entrenches the exact conflation that made the guard
  drift possible, keeps four hand-synced derivations of one concept, and leaves
  `__all__` a name that lies to every Python reader and tool that trusts it.
- **C — a separate runtime registry / decorator.** Express agent visibility
  through a central structure (an `_AGENT_SURFACE` set, or an `@agent_visible`
  decorator) rather than a per-module list. Rejected as more machinery than the
  problem needs: a per-module `__all_for_ava__` list is statically greppable and
  AST-parseable (both lints read it without importing), reads exactly like the
  `__all__` it replaces, and needs no new registration call site — the plugin
  path already appends to a list.

## Consequences

- Two names, cleanly separated: `__all_for_ava__` = agent surface (static
  declaration + runtime plugin mutation); `__all__` = Python re-export, only
  where a module genuinely re-exports.
- The accessor carries one deliberate special case: `ava.mcps`'s per-server
  proxy computes its tool list through a `__all_for_ava__` *property*.
  `agent_visible_names` reads the field with `getattr_static` (so metering never
  force-evaluates a dynamic member, and never triggers an MCP connect), then
  resolves the property through normal access only when it sees one — so
  `help()` still lists a server's tools while metering stays connect-safe.
- Two lints (`lint_doc_symbols`, `lint_agent_docstrings`) and the
  `generate_sdk_changelog` skill now key off `__all_for_ava__`. The doc-symbol
  lint's dunder exemption was widened (`__[a-z_]+__`) so `ava.__all_for_ava__`
  is recognized as protocol surface like `ava.__all__` was.
