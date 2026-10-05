> **Superseded 2026-08-29** — the deepseek pin returned to these same values by
> user decision; the live reference is
> [`2026-08-29-deepseek-compact-thresholds-374k-512k.md`](2026-08-29-deepseek-compact-thresholds-374k-512k.md)
> (the 2026-08-27 600k/700k pin is superseded).

# DeepSeek compact thresholds: 374k soft / 512k hard (per-model exception)

User decision (2026-08-01, task #581): the two DeepSeek models —
`deepseek-v4-pro` and `deepseek-v4-flash` — compact at **soft 374k / hard
512k** absolute tokens, opting out of the flat 30% / 40% rule of
[`2026-07-31-flat-compact-thresholds.md`](2026-07-31-flat-compact-thresholds.md)
for those two entries only.

## What changed

Both deepseek registry entries (`shared/lm/registry.py:MODELS`) now carry

- `auto_compact_fraction=0.512` — hard threshold = 512_000 on their 1M window
- `compact_reminder_fraction=0.374` — soft threshold = 374_000 on their 1M window

The mechanism is untouched: `resolve_context_budget` still derives the absolute
thresholds as fractions of the model's own `context_window`, and the
`AVA_*_FRACTION` / `AVA_AUTO_COMPACT_CEILING_TOKENS` env and per-agent overlay
layers still override these per-model defaults. Every other model keeps the
flat 30/40 rule.

## Why

DeepSeek's own published compaction trigger for these models is 512K (recorded
in the `2026-07-25` evidence table). The flat rule's 40%-of-window hard
threshold lands at 400K on a 1M window — a quarter-million tokens *before* the
vendor's own point, compacting agents earlier than their effective context
justifies. The user chose to pin the deepseek entries back to the vendor-
anchored absolute values, with a soft reminder at 374k.

## Scope

Only the two deepseek entries. The flat rule stays the roster default; the
`2026-07-31` decision's rationale (legibility, one derivable threshold per
model) still governs every other model. The ceiling knob remains unused
roster-wide.
