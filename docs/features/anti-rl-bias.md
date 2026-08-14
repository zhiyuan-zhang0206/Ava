# Anti-RL-Bias

RLHF-tuned models carry two habits that hurt a long-lived agent: treating a text
turn as "I answered, therefore I'm done", and going quiet when unsure. Ava
answers both from the system layer — not with prompt pleading, but with
first-class verbs, daemons, and hooks that turn ambiguous silence into
supervisable state.

## Why it matters

- A fleet that runs unattended cannot afford agents that *look* done when they
  are not, or that stall silently instead of asking.
- The fix has to be structural: an agent that *decides* to declare intent beats
  a prompt that *asks* it to.

## The mechanisms

### 1. Idle heartbeat — the honest-options nudge
`../../services/heartbeat/` · decision `../../decisions/2026-06-22-heartbeat-opt-out-over-escalation.md`

A gateway daemon polls idle agents (15 min interval, 5 min idle threshold) and
sends a nudge naming three honest options: still working (do nothing), waiting
(`ava.self.pause_heartbeat(duration)` — suppresses nudges for a declared
window; real wake-ups still arrive), or done (terminate). Opt-out is an active
agent choice, not an escalation chain.

### 2. Silent-idle continue nudge
`../../ava_builtins/plugins/ava_silent_idle/`

When the model produces reasoning but no text and no tool call, the kernel
keeps the reasoning in context and loops back (no token waste on a blind
re-stream), and a hook injects a nudge: *produce text or a tool call this turn,
or state completion in text*. A per-process consecutive-count guard (default 3)
bounds a model that habitually stalls.

### 3. SDK reminder — steer to the primitive
`../../ava_builtins/plugins/ava_sdk_reminder/`

One-time hints the first time the agent reaches for a native-Python equivalent:
`subprocess`/`os.system` → `ava.shell.run`, `time.sleep` loops → `ava.watcher`,
`open()`/`shutil` → `ava.files`, `requests`/`httpx` → `ava.web`. And when a
peer's message arrives, a before-LLM note reminds the agent that a plain text
reply reaches nobody — the verb is `ava.agents.send_message`.

### 4. Capabilities drift check
`../../agent/hooks/capabilities.py`

Before every LLM call the live skill index is diffed against the snapshot the
context window was built from; skills installed since are named in one system
note. An agent cannot silently rebuild a capability it already has.

### 5. Task progress reminders
gateway tasks API (`remind_interval_seconds`)

The owner of an in-progress task is periodically reminded to post progress.
Reminders cannot be disabled (capped at 24 h) — a task that goes dark is a
visible fact, not a mystery.

### 6. Delivery semantics — silence means "working", not "done"
`../../decisions/2026-08-01-one-send-is-one-inbound.md`

A bare text turn delivers nothing: `ava.agents.send_message` (to peers) and
`ava.ui.notify` (to the user) are the only delivery verbs. So "not done yet"
must be said out loud, and silence reads as busy — the inverse of the RLHF
default.

### 7. Standing memory injection
The memory index (`MEMORY.md`) is injected at cold start and after every
compact: long-lived rules stay in front of the agent instead of decaying out
of context.

### 8. Prompt-layer discipline
`../../conventions/communicating-with-user.md`

Agents give scope and trade-offs, never time estimates, and describe current
behavior — the prompt itself suppresses confident-unverifiable promises.

## Design thread

- Heartbeat as opt-out, not escalation: `../../decisions/2026-06-22-heartbeat-opt-out-over-escalation.md`
- One send is one inbound: `../../decisions/2026-08-01-one-send-is-one-inbound.md`
