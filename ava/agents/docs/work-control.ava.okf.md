---
type: doc
title: SDK Observed Work Controls
description: Explicit observed native-work cancellation/restart and retained original restart progress.
tags: [sdk, agents, idempotency]
---

# SDK Observed Work Controls

```python
target = ava.agents.work.observe(agent_id)
accepted = ava.agents.work.cancel(target, idempotency_key="one-cancel-intent")
```

`observe` reads `/api/keyed/v1/agents/{id}/native-work` and validates the canonical
`NativeWorkTarget`, including the matching agent and exact integer protocol.
Inactive, unmanaged, external or unsupported work is refused by the existing
server admission owner. Observation is a data read, not cached write capability
negotiation. A changed work/owner between observation and acceptance is rejected.

`cancel` accepts the returned target and an explicit valid key. It revalidates
copied/constructed target models before HTTP, then sends the exact target to
the fixed `cancel-work` route with `principal-v1` scope. The canonical returned
`NativeCancelAcceptance` must match that target. The SDK checks an attached
external identity at each HTTP boundary; HTTP authentication and native work
eligibility remain the server's authority. No new server ACL is claimed.

Retain the original target and key for same-intent recovery. Re-observation can
target newer work and must not replace the original retry input. The SDK makes
no automatic ambiguous-failure retry and never falls back to `/api/cancel`.
Connect-family retries preserve the same target/key. A deliberate new command
needs a new key and a separately observed target. There is no client journal,
outbox, retention policy or rollout change.

`work.restart(target, idempotency_key=..., config_overlay=...)` requests restart
of the same observed ACTIVE work through the fixed `restart-work` route. It
uses the existing caller actor owner for attribution and the canonical finite
request model. Retain the original target/key/overlay and caller identity for
replay. The server owns fresh overlay validation/normalization; receipt-first
replay does not rewrite a later overlay or select a successor. A returned
`NativeRestartAcceptance` must match the original work. Unsupported executor
responses are refused by the gateway without legacy fallback.

`work.restart_status(accepted)` reads the original `restart-commands` record
and requires the returned canonical progress to identify that acceptance.
ACCEPTED, APPLIED and OBSERVED are distinct domain outcomes, with retained
original release and real successor-admission timestamps. No current-status
inference or automatic polling is added. The execution and operator
compatibility owner is
[[base/agents/incarnation/docs/native-work-restart.ava.okf.md]].

Each returned receipt proves committed command acceptance, not checkpoint
application, stopped work or completed execution. Native settlement and replay
stay with [[base/agents/incarnation/docs/native-work-cancel.ava.okf.md]].
Other lifecycle consumers, force/idle policies and external cancellation remain
separate from this explicit active-native-work API.
