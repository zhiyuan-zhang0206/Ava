---
name: ava-goal
description: Supervise another agent toward a goal across many turns — a watcher wakes each time the target idles and judges its work against the goal until done. Use to drive a terminal task to completion, or any task that benefits from evaluation when the target stops. Not for perpetual, trigger-driven agents.
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

Goal mode is for terminal work — tasks that finish. Here idle means "stopped too
early," so the move is to nudge it onward. Do **not** put a perpetual,
trigger-driven agent in goal mode (an inbox poller, a daily disk check): its idle
means "finished this round correctly, waiting for the next trigger," so nudging it
is pure harassment. Perpetual work is a `ava-watcher`'s job. To quality-check one such
round, spawn a separate quality-check supervisor that judges *this round's* output
— not a completion driver that says "keep going."

## Procedure

1. **Get a goal and a target agent.** The user usually gives you both. If there
   is no target yet, spawn a worker to pursue the goal:

   ```python
   target_id = ava.agents.spawn(prompt="<the goal, stated as a concrete task>")
   ```

2. **Launch the watcher BEFORE the target starts working.** Read the reference
   watcher from this skill's directory, substitute the target id, and launch it.
   Launch it first so you do not miss the target's idle transition.

   ```python
   watcher = ava.files.read(f"{ava.skills.ava_goal.path}/reference/watch_idle.py")
   watcher = watcher.replace("TARGET_AGENT_ID = 0", f"TARGET_AGENT_ID = {target_id}")
   ava.watcher.launch(watcher, timeout="6h")
   ```

   The watcher subscribes to the target's lifecycle updates and, the moment the
   target goes idle, sends you a reminder. Then it exits (one-shot). `timeout` is
   a safety bound: if the target somehow never idles within it, the watcher stops
   and pings you anyway, so you are never left waiting forever — re-arm if needed.

3. **Idle and wait.** Do not return a tool call this turn. The watcher's
   reminder will wake you when the target idles.

4. **On the reminder, judge and respond — checklist + evidence, not impression.**
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
         "Not done. Defects:
"
         "1. [遗漏] <requirement not implemented at all> — <path:line where it "
         "should be; what you observed instead>.
"
         "2. [误解] <requirement implemented wrong> — <path:line, what the code "
         "does vs what the goal says>.
"
         "3. [偷懒] <check skipped or shallow> — <e.g. test.js has no case for X; "
         "repro: node test.js | grep X>.
"
         "Fix all of the above; anything marked 遗漏 needs a real implementation, "
         "not a comment.",
     )
     watcher = ava.files.read(f"{ava.skills.ava_goal.path}/reference/watch_idle.py")
     watcher = watcher.replace("TARGET_AGENT_ID = 0", f"TARGET_AGENT_ID = {target_id}")
     ava.watcher.launch(watcher, timeout="6h")
     ```

   Categories: **[遗漏] omitted** — a required piece is absent; **[误解]
   misunderstood** — present but does the wrong thing; **[偷懒] slacked** — a
   check or test is skipped, shallow, or fake-green. Keep a per-round log
   (verdict + defects + evidence) — it is the run record you report later.

   Repeat until the goal is met.

## Long tasks

Goals that span many hours need two extra disciplines, both cheap:

- **Worker keeps a progress file** in its OWN workspace (never the artifact
  repo), updated at the end of every round: `DONE` (files/features finished),
  `MISSING` (numbered against the goal), `CHECKS` (latest command results). A
  compacted worker re-reads this file and its goal message to re-anchor — the
  run survives context loss. Require it in the goal's process rules.
- **Supervisor re-anchors on regression**: if the worker repeats finished work,
  asks for context, or reports items as done that its progress file lists as
  done already, its context was likely compacted — re-send the goal message and
  point it at its progress file instead of just listing defects.

## The watcher

`reference/watch_idle.py` listens for the target's "idle" lifecycle signal and
messages you (`ava.agents.send_message`) once when it fires. It is one-shot: re-launch it
each round you still need to keep watching. It reads its connection settings from
the launching agent's own configuration, so it always listens on the same stream
the target reports to. It sets `socket_timeout=None` explicitly (redis-py 8
defaults 5s, which kills the long blocking read the moment the event stream goes
quiet). If the stream dies anyway, it falls back to polling the agents table
(`watch_via_poll`), which also covers the case where the target is already idle
when the watcher starts.

## Topology

Supervision is per (watcher, target), so the shapes compose with no extra
machinery: launch one watcher per target to watch many at once, or have several
watcher agents each watch the same target (e.g. specialist reviewers, each
judging a different aspect). Most goals need just one watcher on one target --
do not over-build.
