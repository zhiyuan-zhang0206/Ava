```
# Long-Running Agent — Health Monitor

**What this shows**: An agent with persistent state, scheduled self-wake-ups,
and proactive user notification — Ava as a background service, not a one-shot tool.

## Prompt

```
You are a health monitoring agent. Load the following skills:
- ava.help(ava.skills.ava_being_a_long_running_agent)
- ava.help(ava.skills.ava_watcher)

Your tasks:
1. Use ava.self.set_label("health-monitor") to set your role
2. Create a daily cron watcher: remind to check health status every morning at 9 AM
3. Maintain your state in ava.memory (under the health/ directory)
4. When awakened by cron:
   - Read the last health record
   - Send daily health reminder to the user via ava.ui.notify
   - If the user hasn't replied for 3 consecutive days, send a P1 priority notification
5. Test: immediately send the first health reminder

This is a long-running agent — do not terminate, go idle after completion.
```

## Expected flow

1. **Setup**: Agent sets its label, creates cron watcher for 9 AM daily
2. **State**: Writes initial health tracking record to `ava.memory`
3. **First check**: Immediately posts a health reminder via `ava.ui.notify`
4. **Idle**: Agent goes idle, waiting for next cron trigger
5. **Day 2**: Cron wakes agent → reads memory → sends reminder
6. **Escalation**: After 3 days without user reply → P1 notification

## Expected output

- A `health/monitor.md` file in the shared memory pool tracking daily status
- Daily `ava.ui.notify` messages with health reminders
- Escalation to P1 if user is unresponsive

## Why this matters

Most agent frameworks are request-response: you ask, they do, they stop.  Ava
agents are **processes** — they can run for days, maintain state, schedule their
own wake-ups, and proactively reach the user.  This is the agent as a service,
not as a function call.
```
