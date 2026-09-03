---
name: ava-watcher
description: Launches background watchers that wake the agent on a condition or deadline. Use when waiting for files, processes, CI, messages, time, or external state, even if polling seems easy.
---

# Watcher

A watcher is a small Python program that runs in the background, independent of
your turn, and sends you a message (`ava.agents.send_message(ava.self.AGENT_ID,
content)`) to wake you whenever its condition is met. You write the condition; the watcher does the
waiting so you don't burn turns polling.

## When to use

- **Custom condition** — watch for anything you can check in Python (a file
  lands, a build finishes, a metric crosses a threshold): write a loop that
  messages you (`ava.agents.send_message(ava.self.AGENT_ID, ...)`) when it
  fires, and
  `ava.watcher.launch(code, timeout, name="<slug>")`.
- **A specific time** — `ava.watcher.at(when, message, name="<slug>")` wakes
  you once at a datetime / after a delay.
- **A recurring schedule** — `ava.watcher.cron(expr, message, name="<slug>")`
  wakes you on a cron schedule until its `end_time` (or you kill its session).

## Custom watcher

Write a program that sends you a message when your condition is met,
then launch it. Launch it BEFORE the thing you're waiting on starts, so you
don't miss the event.

```python
code = '''
import ava
import time

while True:
    out = ava.shell.run("ls /tmp/done.flag 2>/dev/null")
    if out.strip():
        ava.agents.send_message(ava.self.AGENT_ID, "the done.flag file appeared")
        break
    time.sleep(5)
'''
wid = ava.watcher.launch(code, timeout="1h", name="file-watcher")
```

`timeout` is mandatory — a watcher always has a bounded lifetime so a forgotten
one can never run forever. Pass a number of seconds, a `timedelta`, or a
`"<n>{s,m,h,d}"` string. When the timeout elapses the watcher stops itself —
and **every watcher sends you an exit notice when it stops** (exit code, a
pointer to its full output, and the tail of that output — a timeout shows up
there as code 124 with the reason in the tail), so you are never silently
un-watched; re-launch it if you still need it. Set the timeout comfortably
longer than you expect to wait.

Then idle (do not return a tool call) — the watcher's message will wake you.

While it runs, a watcher is a persistent session named with the `name` you
provide: it shows up in `ava.shell.sessions.list()` as `{"id": wid, "name":
"<your-name>"}` (the id is what `launch` returned). Watch it live with
`ava.shell.sessions.capture(wid)`, stop it early with
`ava.shell.sessions.kill(wid)` (killing it discards the exit notice — you
asked it to stop). When the watcher stops on its own, the session closes
itself; the exit notice points at the log file holding its full output.

## Report on change, not on every poll

A watcher's `send_message` wakes you for a full turn, so every message is a
cost even when it carries no news (user ruling 2026-09-03). Two rules keep a
watcher cheap:

1. **Message only when the observed state changed.** Keep the last-seen
   state and compare on every poll; a poll that observes the same state as
   the previous one stays silent. Initialize the previous-state variable
   before the loop so the first poll still counts as a change:

   ```python
   code = '''
   import ava
   import time

   last = None
   while True:
       cur = ava.shell.run("ls /tmp/done.flag 2>/dev/null").strip()
       if cur != last:                  # state changed (or first poll)
           last = cur
           if cur:                      # only interesting states send
               ava.agents.send_message(ava.self.AGENT_ID, "the done.flag file appeared")
               break
       time.sleep(5)
   '''
   ```

   The `last`-comparison is the whole trick: compare the *state*, not the
   clock, and message only on the transitions you care about. A watcher
   that would re-send the same message on the next poll is buggy, not
   cautious.

2. **Prefer event-driven over periodic for anything time-shaped.** A
   periodic heartbeat ("still waiting", "no news yet") is a message that
   exists to say nothing — use `ava.watcher.at` / `ava.watcher.cron` for
   fixed times, and let the automatic exit notice (every watcher sends one
   when it stops) be the "still alive" signal. If you catch yourself
   writing a heartbeat, you are usually waiting on the wrong condition:
   watch the event, not the calendar.

## Time watchers

```python
ava.watcher.at("2026-06-03T09:00:00-07:00", "stand-up reminder", name="stand-up")
ava.watcher.at(datetime.timedelta(minutes=30), "check the deploy", name="deploy-check")
ava.watcher.cron("0 9 * * 1-5", "daily 9am check-in", timezone="America/Los_Angeles", name="morning-checkin")
```

A time watcher occupies one background session that sleeps until its target
time. It does not survive a machine restart — if the host reboots, re-arm any
watcher you still need (your own process is restarted by the framework, but
watchers are not automatically rebuilt).

## See also

The `ava-goal` skill builds on this: it launches an idle-watcher per target agent to
supervise a worker toward a goal across many turns.

The `ava-dynamic-workflow` skill builds on it the other way: its workers finish
silently (write a result file, terminate), and a watcher — one **checkpoint**
per place the orchestrator wants to wake, not one per worker — reports the
whole batch in a single message. Its `reference/gather_files.py` is a ready-made
watcher for that: wake when the named files have landed, or at K of N.
