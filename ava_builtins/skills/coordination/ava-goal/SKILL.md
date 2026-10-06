---
name: ava-goal
description: Sustains pursuit of a terminal goal across turns with evidence-based completion checks. Use when work must continue beyond a first attempt or turn, whether executing directly, collaborating with peers, or supervising delegated work; not for perpetual trigger-driven roles.
---

# Goal Mode

Pursue a terminal outcome across turns. A finished turn, a plausible artifact,
or a peer's completion report does not establish that the goal is met. Keep
working while continuation is authorized, until acceptance evidence supports
completion or you deliberately pause with a handoff.

Reading this skill does not assign you a watcher or worker role. Choose the
arrangement that serves the goal: execute directly, delegate to peers, review
another peer's work, or combine implementation and supervision. Change that
arrangement when useful. Watcher and worker describe responsibilities in one
arrangement; they are ordinary peers, not special agent types.

The watcher/worker loop below is **one available way** to sustain progress. Use
it when supervising another peer is useful; do not spawn a worker or launch an
idle watcher merely because you read this skill. If you execute directly,
apply the same acceptance and continuity disciplines to your own work. A peer
can independently review your results without owning all implementation.

This is a working method, not a special function or framework. The optional
supervision example composes existing messaging and background-wait capabilities.

## Completion and continuity

- Keep a definition-of-done checklist derived from the goal. Verify artifacts
  through appropriate checks, code inspection, or UI use. Reports guide what to
  inspect; they are not acceptance evidence. Name remaining defects with a
  concrete reproduction or other actionable evidence. Claim completion only
  when every required item verifiably passes.
- Keep progress and evidence in durable notes. Re-read the goal, latest progress,
  and handoff after context loss or restart; preserve completed work and peer IDs.
- A budget reminder calls for reassessment, not automatic termination. If you
  pause, save artifacts and record the incomplete goal, verified checks, remaining
  work, relevant peer and watcher session IDs, and the condition for resuming.
  Tell collaborating peers where to read the handoff. Do not continue or re-arm
  supervision while deliberately paused; late notices do not authorize resuming.

Use this method for work that can finish. Idle is a review point, not proof of
failure or permission to keep pushing. Respect deliberate pauses. Perpetual,
trigger-driven roles (such as inbox polling or daily checks) belong in
`ava-guide.schedules`; temporary waits use `ava.watcher`. Review one round of
recurring work against that round's outcome without turning it into a perpetual
completion loop.

## Optional pattern: supervise a peer

In this example, you take the supervising role and another peer executes the
work. Reverse, share, or combine those responsibilities as needed. The steps
below apply only when you choose this pattern.

1. **Choose a target peer for the goal.** Use an existing peer when appropriate.
   If delegation is useful and no suitable target exists, spawn a peer:

   ```python
   target_id = ava.agents.spawn(prompt="<the goal, stated as a concrete task>")
   ```

2. **Launch the watcher as soon as the target ID is known.** Read the reference
   watcher from this skill's directory, substitute the target ID, and launch it.
   Its initial status check also covers a worker that has already gone idle.

   ```python
   watcher = ava.files.read(f"{ava.skills.ava_goal.path}/scripts/watch_idle.py")
   watcher = watcher.replace("TARGET_AGENT_ID = 0", f"TARGET_AGENT_ID = {target_id}")
   watcher_session_id = ava.watcher.launch(watcher, timeout="6h", name="goal-idle")
   ```

   Record the returned session ID with your run notes.

   The watcher subscribes to the target's lifecycle updates and, the moment the
   target goes idle, sends you a reminder. Then it exits (one-shot). `timeout` is
   a safety bound: if the target somehow never idles within it, the watcher stops
   and pings you anyway, so you are never left waiting forever — re-arm if needed.

3. **Wait or pursue other useful work.** The watcher's reminder will wake you
   when the target idles. If there is nothing else to do, return idle; supervising
   a peer does not prevent you from contributing implementation or other checks.

4. **On the reminder, check authority and the latest handoff before evaluating.**
   A budget reminder is a reason to reassess, not a termination command. If you
   or the worker decide to pause, preserve results and write that decision before
   returning idle. Record the goal as incomplete, with the reason, artifact paths,
   verified checks, remaining work, peer IDs, watcher session IDs and the condition
   for resuming (for example a revised budget or a smaller authorized scope).
   Tell the other peer where to read the handoff. Do not automatically re-arm or
   send defect nudges while paused. Late idle/checkpoint/exit notices and a restart
   do not satisfy the recorded resume condition. Read the handoff first after
   recovery; preserve the same IDs and completed work when resuming.

   **While continuing, judge with checklist + evidence, not impression.**
   Keep a definition-of-done checklist (derived from the goal) in your own notes.
   For each item, verify the ARTIFACT yourself: re-run its checks, read the code,
   drive the UI where you can. Every defect you name must carry evidence —
   file path, line number, and a repro command — so the rejection is actionable,
   not a vibe. The target's report is a map of what to check, never evidence
   itself. **Judge conservatively — default to not-done.** Call it *met* only
   when every checklist item verifiably passes.

   - **Goal met** -> tell the target it is done, citing what you verified:
     ```python
     ava.agents.send_message(
         target_id,
         "Goal complete: <what was verified, one line each>. "
         "Deliver the result and record completion.",
     )
     ```
     Completion ends this goal loop. The peer decides its next lifecycle step
     under the applicable instructions; meeting a goal does not require killing
     a persistent peer or preventing it from serving other work.
   - **Not yet** -> send a numbered defect list, each tagged with a category
     and its evidence, then re-arm a fresh watcher (the previous one already
     exited) to wait for the next idle:
     ```python
     ava.agents.send_message(
         target_id,
         "Not done. Defects:\n"
         "1. [omitted] <requirement not implemented at all> — <path:line where it "
         "should be; what you observed instead>.\n"
         "2. [misunderstood] <requirement implemented wrong> — <path:line, what the code "
         "does vs what the goal says>.\n"
         "3. [slacked] <check skipped or shallow> — <e.g. test.js has no case for X; "
         "repro: node test.js | grep X>.\n"
         "Fix all of the above; anything marked omitted needs a real implementation, "
         "not a comment.",
     )
     watcher = ava.files.read(f"{ava.skills.ava_goal.path}/scripts/watch_idle.py")
     watcher = watcher.replace("TARGET_AGENT_ID = 0", f"TARGET_AGENT_ID = {target_id}")
     watcher_session_id = ava.watcher.launch(watcher, timeout="6h", name="goal-idle")
     ```

   Categories: **[omitted]** — a required piece is absent; **[misunderstood]**
   — present but does the wrong thing; **[slacked]** — a
   check or test is skipped, shallow, or fake-green. Keep a per-round log
   (verdict + defects + evidence) — it is the run record you report later.

   Repeat while continuation is authorized, until the goal is met or deliberately
   paused. A preserved partial result is a handoff, not evidence that the goal is met.

## Long tasks

Keep durable progress whether you work directly or delegate. For the supervision
pattern, these responsibilities are useful:

- **Worker keeps a progress file** in its OWN workspace (never the artifact
  repo), updated at the end of every round: `DONE` (files/features finished),
  `MISSING` (numbered against the goal), `CHECKS` (latest command results),
  and any pause decision with its resume condition. A
  compacted worker re-reads this file and its goal message to re-anchor — the
  run survives context loss. When executing directly, keep the same notes yourself.
- **Supervisor re-anchors on regression**: if the worker repeats finished work,
  asks for context, or reports items as done that its progress file lists as
  done already, its context was likely compacted — re-send the goal message and
  point it at its progress file instead of just listing defects.

## The watcher

`scripts/watch_idle.py` listens for the target's "idle" lifecycle signal and
messages you (`ava.agents.send_message`) once when it fires. Delivery retries
across a gateway / agent restart window (doubling gaps, ~10.5 min); if every
attempt fails the watcher exits 2, so the loss surfaces in its exit notice. It
is one-shot: re-launch it
each round you still need to keep watching. It reads its connection settings from
the launching agent's own configuration, so it always listens on the same stream
the target reports to. It sets `socket_timeout=None` explicitly (redis-py 8
defaults 5s, which kills the long blocking read the moment the event stream goes
quiet). If the stream dies anyway, it falls back to polling the target's status
(`_watch_via_poll`), which also covers the case where the target is already idle
when the watcher starts.

## Topology

When using supervision, each idle watcher observes one target, so the shapes
compose with no extra machinery: launch one watcher per target to watch many
at once, or have several peers each watch the same target (e.g. specialist
reviewers, each judging a different aspect). Choose only the coordination the goal needs;
one agent working directly may be sufficient. No topology is required.
