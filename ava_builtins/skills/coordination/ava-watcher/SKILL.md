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
  `end_time` defaults to **now + 7 days**: a standing schedule expires unless
  you register it again before then — calling `cron()` again with the same
  expression and timezone does NOT renew or replace anything, it starts a
  second, independent session, so kill the old one yourself
  (`ava.shell.sessions.kill`) if you don't want both running. A longer
  schedule must pass an explicit `end_time`.

## Usage budget reminders

For agent tokens or recorded API costs, read [usage reports](references/usage.md)
and use `scripts/agent_usage.py`. Select explicit IDs, a time window or lifetime,
and spawn/fork birth lineage; task records are not a spending ledger. Optional
polling sends a one-shot reminder to named peers and exits, without termination.

A reminder is a decision point like a compaction warning. Preserve useful work
and recovery notes, converge, hand off, or seek a revised budget according to
the remaining work and authority. Bound the watcher lifetime and retain its
session identity if it needs cancellation or recovery.

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
and **every watcher sends you an exit notice when it stops on its own** (exit
code, a pointer to its full output, and the tail of that output — a timeout
shows up there as code 124 with the reason in the tail); re-launch it if you
still need it. Set the timeout comfortably longer than you expect to wait.
That covers a watcher stopping itself — see "Time watchers" below for the
cases where the platform ends it instead, some of which are silent.

Then idle (do not return a tool call) — the watcher's message will wake you.

While it runs, a watcher is a persistent session named with the `name` you
provide: it shows up in `ava.shell.sessions.list()` as `{"id": wid, "name":
"<your-name>"}` (the id is what `launch` returned). Watch it live with
`ava.shell.sessions.capture(wid)`, stop it early with
`ava.shell.sessions.kill(wid)` (killing it discards the exit notice — you
asked it to stop). When the watcher stops on its own, the session closes
itself; the exit notice points at the log file holding its full output.

## Fail loud, not silent

A watcher is only useful while you can tell "condition not met yet" from
"probe broken". The exit notice covers a watcher that *stops*; nothing covers
one that goes *blind* while still running, so make blindness loud yourself
(this is not the heartbeat the next section rules out — it is one message at
startup, plus alerts when the probe itself fails):

- **Send a baseline at startup.** One message with the current reading
  ("watch armed for PR #N — state: OPEN"; when the reading comes from a copy
  or an API, include its as-of timestamp). It proves the probe and the notify
  path both work, and makes later silence unambiguous: nothing changed.
- **Never flush a probe error into "unmet".** A `try/except` that folds the
  exception into the value being tested (`data = f"probe error: {exc}"`, then
  `if "MERGED" in data`) keeps looping quietly while nothing is being probed.
  Count consecutive probe failures and send a throttled alert after 3; if the
  target is definitively gone, message and exit non-zero.
- **Keep `cwd` and probe targets on stable paths.** A worktree or temp dir
  can be cleaned up under you; once it is deleted, any probe that passes it
  as `cwd=` (or resolves paths through it) raises `FileNotFoundError` on
  every poll — silently, forever, if the loop swallows it.
- **Refresh what you read.** If the probe reads a local clone or copy, fetch
  first (or read the remote ref) — a stale copy reads exactly like "no
  change".
- **Protect the wake delivery across a restart window.** A gateway / agent
  restart window (an update wave, the fleet update) refuses connections
  for minutes; a bare `send_message` exhausts the SDK's own 3 quick retries
  and the raised exception kills the watcher — the wake is lost silently.
  When the wake must not be lost, retry delivery with growing gaps and fail
  loud: this repo's reference watchers retry 10s to a 160s cap (~10.5 min
  total) and exit 2 when every attempt failed, so the loss surfaces in the
  exit notice. Keep the retry budget inside the watcher's `timeout`.

## Wake on an actionable condition

A watcher's `send_message` wakes you for a full turn, so every message is a
cost even when it carries no news (user ruling 2026-09-03). Two rules keep a
watcher cheap:

1. **Message when the relevant condition needs judgment or action.** Compare
   the condition, not every raw metric fluctuation. Keep ordinary samples in
   logs; suppress repeated healthy readings and unchanged state. Initialize
   the previous state before the loop, and notify on the first poll only if
   it already meets the condition you are waiting for:

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

   Compare the condition relevant to action, not the clock or raw readings.
   Disk usage fluctuating within a healthy range stays silent; crossing an
   intervention threshold can wake the agent. Include the trigger, evidence,
   and durable record pointer in the message. A watcher
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
time. It is just a shell session — nothing tracks it separately, and nothing
ever restarts it automatically, for ANY reason (a crash, `ava stop`, a
release closing terminals, a machine reboot). Write watcher scripts assuming
they may be cut off at any moment and will not be re-run for you. What you
learn about it depends on how its session ended:

- It exits on its own (fired / timed out) — you get the usual completion
  notice, and that's it.
- You kill it yourself — no extra message; you already have the result.
- The platform reclaims it at its TTL deadline, or a normal `ava stop` or
  `ava restart` (updates included) force-closes a busy terminal — you get a
  message saying so.
- `ava stop --force`, a Windows unit's stop, or the pty-sessions service
  ending with nothing to record it (an external SIGKILL, a power loss) — no
  message in any of these; check `ava.shell.sessions.list()` if a watcher's
  continued presence matters to you.

None of these bring the watcher back. Decide whether to re-create it.

A cron schedule without an explicit `end_time` stops after 7 days (the
standing cap). Calling `ava.watcher.cron(...)` again with the same expression
and timezone does **not** renew or replace the earlier watcher — it starts
another, independent session. If you want exactly one live copy of a
schedule, kill the old session yourself before (or after) registering the
new one.

**A watcher outlives your termination and will wake you again.** Terminating
does not stop or clean up any watcher you left running: it is a session, not
part of your process. Its next fire delivers your wake as a normal message,
and message delivery wakes a terminated agent back up — so a standing cron
keeps re-waking you, fire after fire, for as long as it lives. If you do not
want to be woken again, kill your watchers (`ava.shell.sessions.kill`) before
terminating.

## See also

This skill's own `scripts/watch_idle.py` is a ready-made watcher that wakes
you when a target agent goes idle. Delivery retries across a gateway / agent
restart window, and if every attempt fails the watcher exits 2, so the loss
surfaces in its exit notice.

The `ava-goal` skill builds on this: it launches an idle-watcher per target agent to
supervise a worker toward a goal across many turns.

The `ava-dynamic-workflow` skill builds on it the other way: its workers finish
silently (write a result file, terminate), and a watcher — one **checkpoint**
per place the orchestrator wants to wake, not one per worker — reports the
whole batch in a single message. Its `references/gather_files.py` is a ready-made
watcher for that: wake when the named files have landed, or at K of N.
