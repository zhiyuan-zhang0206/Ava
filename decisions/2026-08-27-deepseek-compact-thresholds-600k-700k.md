# DeepSeek compact thresholds: soft 600k / hard 700k

## Context

The deepseek entries pinned at soft 374k / hard 512k (task #581,
[`2026-08-01-deepseek-compact-thresholds.md`](2026-08-01-deepseek-compact-thresholds.md))
were measured against the flat 30/40 rule and DeepSeek's published trigger, not
against Ava's own production traffic. A 2026-08-27 fleet analysis (200 recently
active workers, per-call `llm_usage` accounting) closed that gap:

- Observed per-call input tops out at **513,831** tokens in the sample: one
  worker ever exceeded 512k, none exceeded 600k.
- Compactions are real but mostly voluntary: 19 compact events across 14 of the
  199 workers in the 17h window (25 events / 16 workers in 24h; 150 events
  fleet-wide over 7 days) — several times what the threshold alone predicts, so
  the soft threshold is not today's binding constraint.
- DeepSeek cached-read pricing (~$0.022/M, a 30× read discount) makes a larger
  prefix nearly free to re-read: letting the four workers whose prefix passed
  374k keep it instead of compacting costs ~$1.1 per 17h (+77.8M re-read tokens,
  ~26min of prefill at the measured ~50k tok/s cached-read throughput), and the
  saved summary passes are worth ~$0.04.
- The real cost of compacting is context loss: a ~1.3k-token summary replaces
  the full history, and any detail the summary missed is re-derivation work.
  Measured post-compact behavior: cache ratio drops 99.9% → 92.7% on the first
  call after a compact and recovers by the third (~99.3%), and the cold pass
  itself costs ~$0.001 — the economic question is purely how much rework each
  compaction causes, and that is the part a higher threshold removes.

The user weighed this and chose a middle step: soft 600k / hard 700k.

## Decision

The three deepseek registry entries (`deepseek-v4-pro`, `deepseek-v4-flash`,
`deepseek-v4-flash-vision-exp`) pin

- `compact_reminder_fraction=0.6` — soft threshold = 600_000 on their 1M window
- `auto_compact_fraction=0.7` — hard threshold = 700_000 on their 1M window

Every other model keeps the flat 30/40 rule of
[`2026-07-31-flat-compact-thresholds.md`](2026-07-31-flat-compact-thresholds.md).
The ceiling knob stays unused roster-wide.

## Alternatives rejected

- **748k soft / 840–880k hard** (the analysis' upper recommendation): covers
  every observed input (max 513,831) with no forced compactions at all. Rejected
  as the first step in favor of the more conservative 600k/700k — it still
  removes the forced-compact pressure for everything below 700k while keeping a
  reminder→compaction gap before the 1M window, and the user preferred an
  incremental move.
- **970k soft / hard above it**: zero incremental effect in the sample (nothing
  grows past ~698k), and hard would sit within ~25k of the 1M window with
  observed per-turn growth up to 159k tokens — an overflow risk for no gain.
- **Flat rule for everyone**: rejected — raising other models' thresholds to
  0.6/0.7 of their windows would change models with smaller windows
  (e.g. Anthropic entries) whose cached-read pricing is 9–23× DeepSeek's and
  whose cache TTL makes large prefixes strictly more expensive; their economics
  point the other way. Only the deepseek entries move.
- **Env-level `AVA_COMPACT_REMINDER_FRACTION` / `AVA_AUTO_COMPACT_FRACTION`**:
  cluster-wide, would hit every model — conflicts with the per-model scope above.

## Consequences

- DeepSeek agents compact less often and lose less context; the fleet pays a
  measured ~$1.1/17h (~$1.6/day) extra in re-reads at current traffic — the
  accepted price of fewer summary-induced rework passes.
- The 1M-window headroom above the 700k hard limit is ~300k — enough for the
  observed per-turn growth (p99 7.3k, max 159k) without force-compacting
  mid-turn.
- The earlier task #581 pin is superseded; this file is the live reference for
  the deepseek thresholds.
- If traffic shifts toward longer workers (e.g. CodeAct-merged turns grow the
  prefix faster), the 748k option remains available as a follow-up with the
  same evidence base.

<!-- Evidence: 2026-08-27 fleet analysis, agent #1289 — codeact-bench/ dataset in
     ~/.ava/workspaces/1289/codeact-bench/ (q6_limit_counterfactual.py,
     q5_pull_durations.py). -->
