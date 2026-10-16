# ava-immersive evaluation

Read [the shared contract](../../../platform/ava-guide/references/evaluation.md)
before running these cases. `evals.json` contains constructed live-session
scenarios; all data is inline in each prompt, and only writes under an isolated
scratch directory are permitted.

## Setup and baseline

Candidate: an agent with the ava-immersive skill available, in a session the
human started (cases 1, 3, 4, 5) or did not start (cases 2, 6). Baseline: the
same scenarios without the skill available. Hold the executor model, effort,
tools, and scratch layout fixed. Execution needs an agent surface that can act
and emit messages over time; where only a single static answer can be produced,
grade the planned actions and messages in that answer and record the
limitation rather than claiming a behavioral pass.

## Measures and acceptance

- Reply-gap distribution (cases 1, 3, 5): every gap between agent messages in
  an active session stays within the announced cap, or is covered by a
  pre-notice naming the overrunning step. No unexplained gap beyond the cap.
- Information density: share of session messages carrying a finding, delta,
  decision, or next step; filler count is zero on a manual transcript read.
- Delegation discipline (cases 1, 3, 5): every step expected to exceed one cap
  window is moved to a worker or background job before it runs; no blocked
  long calls.
- Human-first latency (case 3): a new human message is answered within one
  small step.
- Contract completeness (cases 1, 5): cap, split, and exit all named in the
  entry announcement.
- Task quality under the cap (all session cases): the session's deliverables
  still pass the same checks as outside the mode, and delegated results are
  verified before they are reported; pacing never substitutes for correctness.
- Exit reversion (case 4): close summary present; no cap behavior after exit;
  default batched reporting stated.
- Trigger discipline (cases 2, 6): no self-entry; no immersion behaviors
  without a human-started session.
- Discovery (cases 5, 6): a live-session request phrased without the skill's
  literal name selects the mode (positive); an attention-protection request
  ("don't ping me; one summary when done") keeps the default batch discipline
  (negative; adjacent to reduce-context-switch-for-human).

Acceptance: every assertion passes across valid completed cases; blocked or
unexecuted cases leave acceptance incomplete. Efficiency measures (elapsed
time, tokens) are diagnostic when the executor provides them, not substitutes
for the acceptance checks.

## Evidence status

- 2026-10-17 initial revision: cases and measures saved; no session executed
  yet; an independent reviewer read the diff (no blocking findings; follow-ups
  folded). The first live-session run is planned with the skill's requester;
  its transcript evidence is attached here (or in the PR) before any merge.
