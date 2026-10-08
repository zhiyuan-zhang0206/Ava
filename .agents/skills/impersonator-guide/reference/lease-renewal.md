# Lease renewal

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Renewal: only when the Ava side reminds you

The lease TTL is the recovery boundary: if you die or the connection breaks,
control must come back to the Ava agent when the lease expires. That is why
renewal is an explicit, human-scale action and never an automated loop. A
background renewer once kept a dead session's identity alive for hours —
renewing every hour for a 24-hour TTL — until control was lost and the agent
hung. Do not recreate that failure mode.

The correct model:

1. Before expiry, Ava pushes one `reminder`: the lead time is **10% of the
   current TTL or five minutes, whichever is longer**, capped at that TTL.
   Short leases may be reminded immediately; delivery follows the reaper scan.
2. Acknowledge the reminder as soon as it arrives — receipt, like any other
   message — then decide: renew once, or start wrapping up.
3. To renew, extend from now for the time you still need:

```bash
ava impersonate renew <session_id> --agent <agent_id> --ttl 1800
```

The reminder's own renew command repeats your current window as a starting
point; set `--ttl` to what the remaining work needs.

Estimate the TTL short — pick the smallest window that covers the work ahead
(1..86400 seconds; the clock restarts at the moment you renew). The same rule
applies when a lease is requested up front: for a task that looks like about
an hour, ask for about 30 minutes and extend in steps — several short renewals
are the intended pattern, not a failure. Each renewal is a deliberate liveness
check, and a short window is the backstop that returns control to the Ava
agent soon after the session dies instead of parking the agent for a long
span. `--ttl` is required: state the length you need outright. After renewing, keep working
— the next reminder comes before the new expiry if the work is still running.

Hard rules:

- Renew **only in response to a renewal reminder**. No scheduled renewal, no
  "renew every hour just in case", no chained renewals without a fresh
  reminder, no background renewal process. If no reminder has arrived, you do
  not renew — the Ava side times reminders to the actual lease.
- If the lease ends while you work — TTL expiry, or the Ava side stopping a
  takeover after confirmed executor death — stop immediately: further CLI and SDK
  calls fail validation. Control returns to the Ava agent with your
  unacknowledged messages and staged state preserved. Do not keep acting
  under the identity, and do not request a new lease on your own — a fresh
  takeover, if wanted, is arranged by the Ava side. Hand back honestly instead:
  release with a summary whenever you still can, naming every unfinished piece,
  acknowledged or not; if expiry catches you, anything unacknowledged stays for the agent.
