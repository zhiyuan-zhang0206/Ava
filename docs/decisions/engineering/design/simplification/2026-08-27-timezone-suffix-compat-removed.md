# Pre-cutover timezone-suffix compat note removed from the agent prompt

## Context

The 2026-08-22 timezone cutover made the cluster clock the wall clock for
every agent-facing timestamp (see
decisions/2026-08-27-cluster-timezone-wall-clock.md): stamps render in
`settings.general.timezone` with no `%Z` suffix, declared once by the
cluster-timezone context note at rank 15. Messages persisted before that
cutover carry explicit PDT/PST suffixes with US-Pacific wall-clock values
(UTC-7 / UTC-8), and the note told agents to convert them before comparing
with current timestamps (Shanghai = PDT + 15h / PST + 16h).

User ruling 2026-08-27 (Task #1834): delete that conversion hint from the
system prompt. A 15-hour delta between a pre-cutover historical stamp and the
cluster clock never caused a problem in practice.

## Decision

Remove the compat sentence from `_TIMEZONE_FRAMING` in
`agent/graph/_context_notes.py` and delete its pinning test
(`test_warns_about_pre_cutover_pacific_history`). No fallback and no reworded
shorter hint: the note keeps declaring the cluster timezone, stamps stay
suffix-free, and pre-cutover history is treated like any other historical
artifact — a stamp is read in its own terms, not converted (PR #789).

## Alternatives rejected

- **Keep a trimmed hint** ("pre-cutover stamps may differ from cluster time")
  — rejected: the user's ruling is no-fallback (2026-08-23 ruling), and a hint
  that names a conversion without providing it is noise.
- **Move the hint into docs only** — rejected: the instruction had exactly one
  consumer, the prompt itself; the shared memory-pool note that mirrored it
  (`ava/bugs/exec-timestamp-pdt-label-20260822.md`, "How to apply") was
  updated in place instead.

## Consequences

- The timezone context note is one sentence shorter (prompt bytes in the
  stable rank-15 cache band shrink slightly).
- Historical PDT/PST stamps remain in pre-cutover message history; agents
  treat them as self-contained values that were correct at the time and do
  not convert them.
