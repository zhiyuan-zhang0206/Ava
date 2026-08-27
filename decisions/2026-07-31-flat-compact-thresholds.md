# One flat compaction rule: 30% soft / 40% hard of every model's window

Operator decision: every model in the roster compacts on the same two
percentages of **its own** context window — a wind-down reminder at 30%
occupancy, a forced compaction at 40%. No model carries a compact fraction or an
absolute ceiling of its own.

This replaces the per-model tiers of
[`2026-07-25-per-model-tuning-values.md`](2026-07-25-per-model-tuning-values.md),
where each entry's threshold was anchored on that lab's own published trigger
(Anthropic 150K, Codex ~245K, Moonshot 300K, DeepSeek 512K) or its published
degradation curve (gemini 0.5, mimo 0.25, grok 0.4).

## What changed and what did not

The mechanism is untouched: thresholds are still
`min(auto_compact_fraction × window, auto_compact_ceiling_tokens)` for hard and
`compact_reminder_fraction × window` for soft, still resolved per model through
`resolve_setting`'s layering, still compared against the last LLM call's real
`input_tokens`. Only the values moved — `DEFAULT_TUNING` now carries 0.4 / 0.3,
and every per-model compact override was removed so the floor is what every
model resolves to.

The ceiling knob stays in place and stays at 0 everywhere. It is the escape
hatch if an absolute trigger is ever wanted again — per model in the registry,
or cluster-wide via `AVA_AUTO_COMPACT_CEILING_TOKENS` — so reverting to
evidence-anchored tiers is a values edit, not a mechanism rebuild.

## Why a flat rule

The per-model tiers were defensible individually but produced a roster where the
compaction point was a different fraction of the window on every model, tracking
each vendor's publication habits rather than anything the operator controls.
One rule is legible: the threshold for any model is derivable from its window
alone, and a model added to the registry inherits it without a tuning entry.

The direction of the change is uniformly tighter or equal — 40% of window is at
or below every prior threshold except on the small-window Claude entries, where
the prior absolute ceilings (150K on a 200K window) sat above it. Nothing gets a
looser threshold than it had.

## Trade-off accepted

A percentage of the advertised window is exactly what the 2026-07-25 entry
argued against: advertised windows grew ~8x while measured effective context did
not, so a vendor window inflation loosens this threshold automatically. At 40%
that pressure is far weaker than at the old 0.8 shared floor, and the ceiling
knob remains as the pin. If a future window inflation makes 40% implausible on
some model, the fix is a ceiling on that entry — or a new decision moving the
flat rule — not a return to tracking vendor announcements per model.

<!-- Superseded for deepseek only by:
decisions/2026-08-01-deepseek-compact-thresholds.md — user decision
(2026-08-27, superseding task #581's 374k/512k) pins the deepseek entries at
soft 600k / hard 700k (0.6 / 0.7 of their 1M window). Every other model still runs this flat
rule. -->
