# DeepSeek compact thresholds: 374k soft / 512k hard (re-pinned 2026-08-29)

## Context

The deepseek registry entries have carried user-pinned compact thresholds
since 2026-08-01 (task #581): soft 374k / hard 512k
([`2026-08-01-deepseek-compact-thresholds.md`](2026-08-01-deepseek-compact-thresholds.md)).
On 2026-08-27 the user moved them to soft 600k / hard 700k, based on a fleet
analysis (200 recently active workers; observed max per-call input 513,831;
DeepSeek cached-read pricing making larger prefixes cheap to re-read — see
[`2026-08-27-deepseek-compact-thresholds-600k-700k.md`](2026-08-27-deepseek-compact-thresholds-600k-700k.md)).

Two facts reshaped the picture since:

- PR #903 (heartbeat circuit breaker + overflow auto-compact self-rescue,
  merged 2026-08-29) disclosed that the 700k hard threshold sits above the
  provider's effective input ceiling (~664k = 1M window − 384k completion
  budget), so under the 2026-08-27 pin the forced-compact path could never
  fire on the deepseek entries — the breaker's overflow self-rescue was the
  only path out of a context-length rejection.
- The user, ruling in person on 2026-08-29 11:58 (Beijing time), chose to
  revert to the earlier pin (soft 374k / hard 512k).

## Decision

The three deepseek registry entries (`deepseek-v4-pro`, `deepseek-v4-flash`,
`deepseek-v4-flash-vision-exp`) pin

- `compact_reminder_fraction=0.374` — soft threshold = 374_000 on their 1M window
- `auto_compact_fraction=0.512` — hard threshold = 512_000 on their 1M window

Every other model keeps the flat 30/40 rule of
[`2026-07-31-flat-compact-thresholds.md`](2026-07-31-flat-compact-thresholds.md).
The ceiling knob stays unused roster-wide.

Prod already runs these values through the env layer
(`AVA_AUTO_COMPACT_FRACTION=0.512` / `AVA_COMPACT_REMINDER_FRACTION=0.374`,
cluster-default); this decision aligns the code-layer registry default with
that state, so a machine that drops the env vars falls back to 374k/512k
instead of the 2026-08-27 600k/700k pin.

## Alternatives rejected

- **Keep soft 600k / hard 700k** (the 2026-08-27 pin): the hard threshold sits
  above the provider's effective input ceiling (~664k), so the forced-compact
  path can never fire on these models — the pin's headroom was unreachable in
  practice. Rejected by the user in favor of the earlier values.
- **748k soft / 840–880k hard** and **970k soft** (the 2026-08-27 analysis'
  upper recommendations): both keep the hard threshold at or above the same
  ~664k effective ceiling; the full alternatives analysis stays on record in
  the superseded 2026-08-27 document.
- **Flat rule for everyone / env-level fractions**: unchanged from the
  2026-08-27 analysis — roster-wide or cluster-wide changes would hit models
  whose cached-read economics point the other way; the pin stays scoped to the
  deepseek entries.

## Consequences

- DeepSeek agents compact at soft 374k / hard 512k again. The hard threshold
  now sits **below** the provider's effective input ceiling (~664k), so
  auto-compact can actually fire on these models; #903's circuit breaker and
  overflow self-rescue remain as the fallback for the permanent-400 path,
  unchanged by this pin.
- The fleet pays more cached-prefix re-reads and loses context earlier than
  under the 2026-08-27 pin — the accepted price of the user's choice; the
  2026-08-27 analysis quantified both directions and stays on record in the
  superseded file.
- The task #581 pin is re-affirmed with the same values; this file is the
  live reference for the deepseek thresholds.

## Supersedes

- [`2026-08-27-deepseek-compact-thresholds-600k-700k.md`](2026-08-27-deepseek-compact-thresholds-600k-700k.md)
  — its fleet analysis and alternatives remain on record there.
- The `2026-08-01` decision (task #581) is re-affirmed with the same values.
