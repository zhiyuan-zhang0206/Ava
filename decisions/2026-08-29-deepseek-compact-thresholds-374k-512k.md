# DeepSeek compact thresholds: 374k soft / 512k hard (re-pinned 2026-08-29)

User decision (2026-08-29 11:58, Beijing time): revert the deepseek compact pin
to **soft 374k / hard 512k** absolute tokens — the values of
[`2026-08-01-deepseek-compact-thresholds.md`](2026-08-01-deepseek-compact-thresholds.md)
(task #581), superseding the 2026-08-27 move to soft 600k / hard 700k.

## Decision

The three deepseek registry entries (`deepseek-v4-pro`, `deepseek-v4-flash`,
`deepseek-v4-flash-vision-exp`) pin

- `compact_reminder_fraction=0.374` — soft threshold = 374_000 on their 1M window
- `auto_compact_fraction=0.512` — hard threshold = 512_000 on their 1M window

Every other model keeps the flat 30/40 rule of
[`2026-07-31-flat-compact-thresholds.md`](2026-07-31-flat-compact-thresholds.md).
The ceiling knob stays unused roster-wide.

## Deployment state

Prod already runs the reverted values through the env layer
(`AVA_AUTO_COMPACT_FRACTION=0.512` / `AVA_COMPACT_REMINDER_FRACTION=0.374`,
cluster-default), so no runtime change is required. This decision aligns the
code-layer registry default with that state, so a machine that later drops the
env vars falls back to 374k/512k instead of the 2026-08-27 600k/700k pin.

## Supersedes

- [`2026-08-27-deepseek-compact-thresholds-600k-700k.md`](2026-08-27-deepseek-compact-thresholds-600k-700k.md)
  — its fleet analysis and alternatives remain on record there; the user chose
  to return to the earlier, more conservative pin.
- The `2026-08-01` decision (task #581) is re-affirmed with the same values;
  this file is the live reference for the deepseek thresholds.
