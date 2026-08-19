# Philosophy

Why Ava is shaped the way it is. `AGENTS.md` "Core principles" is the operational
list — what to do when writing code. This is the single thread underneath it:
only the *why* lives here, the *how* stays in AGENTS.md.

## The bet

Ava bets the model keeps getting stronger. So every layer is built asking: **can
this scaffolding be stripped later, once the model no longer needs it?**
(*Removable*: output parsing, retry, middleware, SDK surface, long-term memory.
*Not removable*: persistence, wake, and — once one is built — an execution
sandbox; Ava has none today (see [`SECURITY.md`](../SECURITY.md)), but the
bet is that a real isolation boundary stays load-bearing even as the model
gets stronger, unlike scaffolding built to cover for a weaker model.)

## The thread

> **Use the strongest invariant that kills the ambiguity, rather than adding
> machinery to manage it.**

Ambiguity — compatibility judgements, fallback branches, in-between states — has
to be babysat by machinery. An invariant makes it not exist. Ava picks the latter
every time:

| Axis | Not chosen (manage ambiguity) | Chosen (strong invariant) |
|---|---|---|
| Tooling | multi-tool dispatch + per-tool JSON schema | one `execute_code` |
| DB schema | range compatibility (`<=`) | strict equality, raises both directions |
| Cluster version | cross-version compatibility matrix | (proposed) commit-level pinning |
| Model output | parser fallback / `or {}` / `case _:` | let it crash, feed the error back |

## Corollaries

1. **Fail-fast is system-level, not just code-level.** AGENTS.md lists the
   code-level cases (parser fallback, `or {}`, `case _:`, "rare but possible"
   comments). The same rule extends to system state: cross-version runtime, a
   stranded paused posture, a half-applied schema — all ambiguous
   in-between states to eliminate, not tolerate.

2. **A closed system is designed as a closed system.** Every node is self-owned,
   controllable, re-spawnable. So enforce consistency rather than inherit the
   open-API reflex of "tolerate drift / stay backward compatible". Tell apart the
   **boundary you don't control** (genuinely needs compatibility — e.g. model
   output) from the **interior you fully control** (should be strictly consistent
   — e.g. cluster version).

3. **Dissolve the problem before solving it.** Commit-level cluster pinning makes
   "are these two versions compatible?" *disappear* rather than answering it.
   That is the real logic of small-core — not "less code", but "less ambiguity to
   babysit".

4. **A compatibility shim hides the problem** (already in AGENTS.md; here is how
   it ties in). A shim buries the real issue in the prompt / data / interface; an
   invariant that breaks should break. Two sides of the same coin.

5. **Fail-fast when a learner is in the loop; reconcile when there is none.**
   Model mistakes, operator mistakes, our own bugs — fail loudly, so the signal
   reaches something that can fix the cause. World drift (a process died, an ACL
   vanished, a pin drifted) has no learner; crashing converts drift into outage —
   reconcile toward explicit desired state instead. Never self-heal our own
   bugs; every reconciler points at a declared spec dimension, heals loudly, and
   chronic healing escalates — a reconciler that fires constantly is masking a
   bug. (Decision: fail-fast vs reconcile boundary — see git log)

6. **Plugins are the quarantine zone for model-weakness shims.** Core never
   shims (rule 4); a shim that pays for itself today lives in a plugin,
   removable, maintained at full standard while alive, and measures its own
   obsolescence (activation telemetry per model) — "removable" as a gauge, not
   a vibe. The gauge is `shared/plugin_activation.py`: every plugin hook, wrap,
   and prompt section that fires emits one event carrying the model in force,
   counted per contribution by the `plugin_activation` metric.

In one line: every design choice asks *"can a stronger invariant make this
problem not exist?"* Strong model + strong invariants = the least scaffolding.
