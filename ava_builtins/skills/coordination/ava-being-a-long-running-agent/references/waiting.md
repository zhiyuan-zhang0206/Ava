# Waiting and monitoring

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Wait with watchers, never with loops

When you are waiting on an external event, arm a watcher and idle. A few common
cases:

- **Peer agent reply**: a message addressed to you wakes you — nothing to poll.
  `ava.agents.get_last_message(target)` is not a reply signal: it returns the
  peer's last AI *turn text*, `None` when that turn had no text (e.g. the peer
  answered by `send_message`, task #3656). Poll it only when the signal you
  await genuinely is turn text; for liveness use `ava.agents.get_status(target)`.
- **Scheduled time**: `ava.watcher.at(...)`.
- **File to land**: poll `os.path.exists(...)` in a custom watcher.
- **Temporary recurring model check**: `ava.watcher.cron(...)`; durable recurring work belongs in a schedule.

Read `ava.help(ava.watcher)` for watcher contracts and
`ava.help(ava.shell.sessions)` to inspect or stop the returned session.
Use temporary watchers for a bounded wait you own. Use
`ava.skills.ava_guide.schedules` for recurring work that must resume after an
interruption. Peer messages already provide a wake path; do not add a watcher
when the existing delivery meets the need.

Record a baseline when arming a custom probe, along with its target and as-of
time. Wake only when the awaited condition needs action, including if it already
holds on the first check. Keep healthy samples in logs. Treat probe failures as
failures, not as an unmet condition; alert after repeated failures and stop when
the target is definitively gone. Use stable paths and refresh copied state before
checking it. Arrange bounded, visible delivery retries when a lost wake would
leave the task unattended.

The bundled `scripts/watch_idle.py` is a reference body for an idle-agent wait.
Read it only when implementing that wait; it uses authoritative status checks
and bounded delivery retries. Do not start a duplicate monitor.

### pause_heartbeat

When you are deliberately waiting (a watcher is armed, a peer is working),
suppress the idle check-in nudge with `ava.self.pause_heartbeat(duration)`. Use
a watcher to know when the wait is over. Do not use one in place of the other —
the heartbeat wake carries no signal about the event you are waiting for. And
do not use either in place of ending yourself: when the wait is over and the
task is done, terminate — do not re-pause the heartbeat.

#### Exponential backoff

Each `pause_heartbeat` call and each heartbeat wake costs a turn — the model
runs, a token budget is consumed. When the user or a peer is away for hours or
days, a fixed-duration pause (e.g. 1h) causes many wasted turns. Use exponential
backoff to stretch the pause window while keeping the agent reachable:

| Consecutive idle turns | Pause duration |
|------------------------|---------------|
| 1st | 1 hour |
| 2nd | 2 hours |
| 3rd | 4 hours |
| 4th+ | 8 hours (cap) |

**How to track**: count how many consecutive turns you have idled without
performing meaningful work. Each time you wake up, check your watchers or
pending messages. If nothing has changed, increment the idle count and pause
with the next duration in the sequence. When you actually do work — process a
message, act on a watcher firing, deliver a result — reset the count to zero.

**Rationale**: this example reduces repeated idle check-ins. Choose the cap from
the required response time; it is not a guaranteed token saving or delivery bound.

**Trade-off**: polling intervals determine how soon a watcher detects a condition.
Heartbeat pauses suppress check-ins, not delivery of messages or watcher events.
Do not rely on a heartbeat wake as the signal for an awaited event; arrange its
own delivery and choose polling intervals to satisfy the response requirement.

### Monitoring without a model turn on every tick

Use `ava.watcher.cron` or a schedule when the recurring work itself needs model
judgment. For mechanical CI, file, queue, or health checks, use a custom background
watcher that checks the condition and sends a message only when you must act.
Read `ava.help(ava.watcher)` for API semantics. Set the interval and lifetime from
the response requirement, and reuse existing event delivery or a monitor when it
already covers the wait.

Compare the condition relevant to action, not raw readings: disk usage moving
within a healthy range is not a wake trigger; reaching the intervention threshold
is. Keep ordinary samples in a log. A wake message should name the condition,
the relevant evidence, and the durable record to resume from.

When the wait resolves or is cancelled, stop the owned monitor if it is no longer
needed. Keep recurring role monitors while that role remains active. On recovery,
inspect recorded monitor references and current status before replacing them.

### Coordinating a wait with peers

Use the fleet communication contract for milestones, blockers, commitments, and
handoffs. When another agent relies on your acceptance or timing, send that
commitment with the useful update; do not send a preliminary status solely because
you are about to work for a long stretch. Persist intermediate progress in the
task file so recovery does not depend on a sequence of messages.
