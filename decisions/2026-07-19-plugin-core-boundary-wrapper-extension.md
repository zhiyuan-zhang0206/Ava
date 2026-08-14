# Plugin/core boundary and the wrapper extension model

## Context

The 2026-07 knowledge-graph refresh surfaced how far plugins reach into the
core namespace: `ava_fleet` provides the whole `ava.tasks` registry and injects
members into `ava.self` / `ava.ui`; `ava_code` provides `ava.cwd` and wraps
seven SDK functions guarded by a hard "sole wrapper of `ava.files.read`"
assert. This raised three questions: is namespace penetration erosion of the
single-namespace design, where exactly is the plugin/core boundary, and what
mechanism should plugin extension use.

## Decision

**Boundary criterion.** Capabilities that will die as models improve live in
plugins (task tracking, syntax repair, idle nudges). Things tied to deployment
physics live in core (the loop, the namespace machinery, the extension points
themselves, the data plane — and context compaction: context windows are
physics, not a model weakness, which is why compact was deliberately promoted
from plugin to `agent/hooks/compact.py`). Plugins are also the sanctioned
quarantine zone for shims that would violate core philosophy if they lived in
core (`ava_syntax_fix` vs "no fallbacks for model mistakes"): the shim is
tolerable because it is strippable. Quarantine is a philosophy exemption, not
a quality exemption — a live shim is maintained at full standard, and every
shim must measure its own obsolescence (fire-rate telemetry per model; a rate
near zero is the empirical strip signal).

**Injection is the design.** Adding members via `register_namespace_member`
is intended, not erosion: agents see one `ava.*`, operators strip capability
blocks.

**Wrapping stays, and becomes a first-class stack.** Wrappers are
Turing-complete Python and that is the point — a schema'd hook registry would
limit extensions to anticipated shapes, contradicting code-as-action. What
changes is that the stack stops being a folkway:

1. A registration primitive replaces bare setattr (`ava.extend.wrap(target,
   fn)` shape). It does not constrain what the wrapper does; it makes the
   stack enumerable, deterministic, and kills the exclusivity assert (which
   was compensating for the missing primitive). Wrappers compose in
   registration order.
2. Registration order = plugin load order, pinned deterministic and
   user-visible. Most real wrapper conflicts are order bugs; a printable
   stack demotes them to debug output.
3. Lawfulness contract (review-level, not type-level): preserve the inner
   signature (extend by adding kwargs only), never swallow exceptions
   silently, document short-circuits / multi-calls of inner.
4. No precedence tiers (before/around/after) until a real collision between
   two plugins exists.

Accepted cost: "what does `ava.files.read` do here" becomes a runtime
question. The introspectable stack is the mitigation — it also lets
plugin-injection documentation be generated mechanically instead of
hand-maintained in two drifting trees.

## Alternatives rejected

- **Schema'd hook points** — expressiveness-limited, off-brand for a
  code-as-action system.
- **Exclusive single-wrapper rule** — never the intent; breaks composition;
  first-come-wins asserts are what make community plugin ecosystems hell.
- **Building conflict-resolution machinery now** — conflicts need visibility
  and deterministic order, not resolution; revisit on first real collision.

## Consequences

Design detail in `future/plugin-extension-api.md` (since removed — the
primitive shipped; see `ava/_extend.py`): the wrap primitive and
its home (`ava/_extend.py`), strippability CI, a plugins test root, narrowing
the import-linter exemption (its original justification — `ava_compact`'s
intentional cycle with the agent kernel — expired when compact moved into
core), and shim fire-rate telemetry.
