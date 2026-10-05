# Typed boundaries over pyright rules

## Context

An audit of untyped `dict` access across the internal runtime code
(`shared/ gateway/ agent/ ops/ cli/ plugins/ services/`, tests and the `ava/`
SDK surface excluded) went looking for type-safety debt and landed on two
structural facts about `[tool.pyright]` in `pyproject.toml` rather than a pile
of scattered findings:

1. The entire `reportUnknown*` family (`reportUnknownMemberType`,
   `reportUnknownArgumentType`, `reportUnknownVariableType`,
   `reportUnknownParameterType`, `reportMissingParameterType`,
   `reportUnknownLambdaType`, `reportMissingTypeArgument`) was set to `"none"`
   — not merely downgraded to warning. This is the only rule family able to
   flag `x = payload["k"]` degrading to `Any` and propagating silently, so
   turning it off gave every untyped-dict access in the repo zero CI signal.
2. `services/`, `ops/`, and `plugins/` (~15,000 LOC combined) were absent from
   pyright's `include` entirely — not lenient, unchecked. The single riskiest
   wire in the audit, the ops cluster-RPC dispatch (`dispatch_to_machine`
   payload/result/error, all `dict[str, Any]`, hand-packed by the gateway and
   hand-unpacked with manual `isinstance` guards by
   `services/agent_ops/daemon.py`), lives entirely inside this unchecked
   region, spanning both ends of the wire.

The highest-risk concrete finding: `ava_msg_type`, the discriminator for
~24 `ava_*` keys agents write into LangChain's untyped `additional_kwargs`,
is compared as a bare string literal in five call sites across three modules
(`agent/messages.py`, `agent/graph/_claim.py`, `shared/timeline.py`,
`gateway/context_breakdown.py`, `agent/graph/_memory_recall.py`) with no
enum and no exhaustiveness check — a typo or value drift between writer and
reader fails silently (wrong timeline render, wrong context bucket), not
loudly. The same file already had a working answer next to it: `ava_note_tag`
is a `StrEnum`. And `shared/events.py` already modeled its own SSE wire as a
one-`BaseModel`-per-role discriminated union (`role: Literal[...]` +
`EVENT_ADAPTER = TypeAdapter(Event)`) — proof the pattern works here, just not
applied consistently.

## Decision

**Govern this class of debt by modeling the boundary, not by turning on a
pyright rule.** `reportUnknown*` being off is real, but flipping it on
doesn't itself fix anything: an "Unknown" finding is satisfied equally by
annotating the boundary correctly (`TypedDict`/`BaseModel`/`StrEnum`) or by
annotating it away (`dict[str, Any]`, which is exactly the debt in question).
It cannot distinguish "properly modeled" from "silenced," and it cannot catch
the failure mode the audit actually found — a discriminator string drifting
between writer and reader — since both sides already type-check as `str`.
Catching that requires the boundary to actually be a model. The template is
`shared/events.py`'s discriminated union; every fix in this program reproduces
that shape at a different seam.

Eight PRs (#658–#665) landed this program:

- **#659** brings `services/ops/plugins` into pyright's `include`, closing
  the unchecked region (prerequisite — the RPC wire's `services/` side was
  invisible before this).
- **#660** types `ava_*` message metadata: a `StrEnum` discriminator plus a
  `TypedDict` for the kwargs bag, with reads routed through one collapsing
  function instead of five independent `.get()` call sites.
- **#665** models the ops cluster-RPC envelope and per-`OpKind` payloads,
  replacing the hand-packed/hand-unpacked `dict[str, Any]` on both ends.
- **#663** gives LLM content blocks and tool calls (`ContentBlock`,
  langchain's own `ToolCall`) their real shape instead of a widened
  `dict[str, Any]`.
- **#662, #664, #661, #658** cover CLI wire shapes, fleet notice rows /
  `ava_code` findings / tiered `event_log` payloads, fail-fast
  `usage_metadata` handling, and the evals container boundary
  (`CaseResult`, typed `swe_bench` instance) respectively.

## Alternatives rejected

- **Turn `reportUnknown*` to error globally, immediately.** This is the
  obvious reading of "the rule is off, turn it on." Rejected: even setting
  aside cost, an Unknown-type error only forces *some* annotation at that
  call site — it does not force the *correct* one, so it would not have
  caught the `ava_msg_type` bare-string drift that was the audit's top
  finding (both sides are already valid `str`). A later same-night empirical
  probe (§ below) also priced this option and found it independently
  unaffordable as a first move: 11,319 errors, 85% of them in test
  scaffolding with zero bug value. Governance stayed on boundary modeling;
  the rule-flip became a separate, later decision (below) about visibility,
  not about catching this class of bug.

## Decision (addendum, same night): a pyright strictness ladder

The `reportUnknown*` rules were left at `"none"` on the reasoning, written
into the `pyproject.toml` comment, that untyped third-party stacks
(langgraph / redis / psycopg / fastapi / loguru) lack complete stubs and would
drown real errors in noise. An empirical probe against `origin/main @
7bab099a` (a throwaway `pyproject.toml` edit in a detached worktree, flipping
the whole family to `error` and running `pyright` for real numbers) checked
that reasoning and found it stale:

- Every major dependency now ships `py.typed` (langchain_core, redis, psycopg,
  loguru, httpx, fastapi, starlette, anthropic, pydantic, openai, langgraph,
  langgraph_sdk) — `types-*` stub packages have no leverage once a library
  ships inline types, so "stubs are incomplete" no longer explains anything.
- The real total is **11,319** errors (the `~7700` in the comment was already
  out of date), of which **85% (9,644) sit in `tests/`** — monkeypatch
  lambdas, fake helpers, loose-annotated stubs — genuine test-double idiom,
  not debt; annotating it fully is churn with no bug value.
- Non-test code totals **1,675**; the cross-process/cross-layer core
  (`shared+ops+services`) totals just **234**.
- The remaining Unknown propagation traces to three real, cheap-to-name
  sources: our own test scaffolding (above), a handful of upstream libraries'
  unparameterized internal generics (`list[Unknown]` inside an otherwise
  `py.typed` package), and our own lazy-import pattern (module-level type
  annotations referencing names imported inside a function body, since this
  repo bans `if TYPE_CHECKING:`) in four files.

**Decision: replace the blanket `"none"` with a ladder** — `reportUnknown*`
goes to `warning` repo-wide first (free: CI and pre-commit only gate on
`error`, so this adds IDE/log visibility with zero risk of a new gate), then
to `error` directory by directory starting from the highest-signal core
(`shared/ops/services`, 234 findings) and expanding outward by directory
(`gateway` 139 → `agent` 252 → `ava` 362 → …). `tests/` stays at `warning`
permanently — its 9,644 findings are idiomatic test-double style, not a queue
to clear.

## Alternatives rejected (ladder)

- **Flip to `error` everywhere in one step.** Priced at 11,319 errors, 85%
  of which is test-scaffold noise with zero bug value to fix — the annotation
  churn this would force on `tests/` was the deciding number against it.
- **Invest in third-party type stubs.** Measured, not assumed: every current
  dependency ships `py.typed`, and pyright ignores a stub package once inline
  types are present, so `types-*` packages buy nothing here. The residual
  Unknown propagation is ours (test scaffolding, the lazy-import pattern) or
  upstream's own loose internal generics — neither is a stub-shaped fix.
- **Leave the `"none"` + stale-stub-comment status quo.** The comment's
  factual premise (stubs incomplete) no longer holds; keeping it would leave
  the config rationale actively wrong in the file everyone reads first.

## Consequences

- The two decisions are separate axes, not substitutes: boundary modeling
  (top half) catches the specific "shape drifted between producer and
  consumer" bug class that a type-Unknown warning cannot; the ladder (bottom
  half) is what makes *future* regressions into that same debt visible again,
  starting from the same core the modeling program prioritized.
- `services/ops/plugins` are now inside `include` (#659), so the ladder's
  `error` tier can already reach the ops-RPC wire once it steps to that
  directory.
- The `[tool.pyright]` comment block's "third-party stubs incomplete, ~7700
  findings" rationale is now known-stale; implementing the ladder (not done
  by this entry) should also rewrite that comment to the measured numbers
  above rather than carry the old narrative forward.
- Which directory the `error` tier reaches next after `shared/ops/services`
  (`gateway` next at 139, then `agent` at 252, …) is an ordering call left to
  the implementation work, not fixed here.
