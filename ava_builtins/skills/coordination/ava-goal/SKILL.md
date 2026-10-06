---
name: ava-goal
description: Supervises another agent across turns until a terminal goal is achieved. Use when driving a worker to completion, evaluating each idle point, or babysitting a finite task; do not use for perpetual trigger-driven roles.
---

# Goal Mode

Pursue a goal that spans many turns of another agent. You are the watcher: you
launch a background watcher on a target agent, and each time the target finishes
a turn and goes idle you wake up, judge its latest work against the goal, and
either tell it it is done or tell it exactly what is still missing.

This is a procedure you follow, not a function you call. It is built entirely
from existing capabilities -- `ava.agents.spawn` / `ava.agents.send_message` and
`ava.watcher.launch`. There is no special framework support and nothing to
install.

Goal mode is for terminal work — tasks that finish. Idle prompts a review; it
does not itself authorize more work. Continue only while the work remains
authorized and the worker has not deliberately paused. Do **not** put a perpetual,
trigger-driven agent in goal mode (an inbox poller, a daily disk check): its idle
means "finished this round correctly, waiting for the next trigger," so nudging it
is pure harassment. Persistent recurring work belongs in `ava-guide.schedules`;
temporary waits use `ava.watcher`. To quality-check one such round, spawn a separate quality-check supervisor that judges *this round's* output
— not a completion driver that says "keep going."

## Procedure

1. **Get a goal and a target agent.** The user usually gives you both. If there
   is no target yet, spawn a worker to pursue the goal:

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

3. **Idle and wait.** Do not return a tool call this turn. The watcher's
   reminder will wake you when the target idles.

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
         "Deliver and end your own process.",
     )
     ```
     The target's last step is its own: it delivers, then terminates itself.
     If it lingers idle afterwards, `ava.agents.terminate(target_id)` is your
     fallback — but the normal path is the target ending itself.
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

Goals that span many hours need two extra disciplines, both cheap:

- **Worker keeps a progress file** in its OWN workspace (never the artifact
  repo), updated at the end of every round: `DONE` (files/features finished),
  `MISSING` (numbered against the goal), `CHECKS` (latest command results),
  and any pause decision with its resume condition. A
  compacted worker re-reads this file and its goal message to re-anchor — the
  run survives context loss. Require it in the goal's process rules.
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

Supervision is per (watcher, target), so the shapes compose with no extra
machinery: launch one watcher per target to watch many at once, or have several
watcher agents each watch the same target (e.g. specialist reviewers, each
judging a different aspect). Most goals need just one watcher on one target --
do not over-build.
