---
type: doc
title: Explicit SDK Launch Retry
description: Caller-owned observed-attempt retries and immutable historical acceptance without legacy fallback.
tags: [sdk, agents, idempotency]
---

# Explicit SDK Launch Retry

Observe the attempt once, then retain its UUID and the key for this intent:

```python
prior = ava.agents.get_launch_attempt(agent_id)
ava.agents.retry_launch(
    agent_id,
    require_idempotency=True,
    idempotency_key="one-deliberate-retry",
    expected_prior_attempt_id=prior,
)
```

The strong mode posts to `/api/keyed/v1/agents/{id}/retry-launch` with verified
`principal-v1` scope. Both SDK boundaries reject missing/invalid keys, missing
or invalid observed UUIDs, and non-bool admission flags before HTTP. Supplying
the key or observed UUID without strong mode is rejected rather than silently
calling the legacy path. `get_launch_attempt` refuses absent old fields or
invalid observations; it does not negotiate a write capability.

Same-intent recovery reuses the original agent ID, key and observed prior UUID.
It must not refresh the observation before replay: that could change the intent
to a replacement attempt. A deliberately new retry uses a new key and a newly
observed UUID. The existing return type remains the accepted agent ID.
The SDK validates the receipt's agent/prior/new attempt identities and explicit
acceptance/execution flags. Acceptance is historical intent, not runner readiness
or execution evidence, and survives deletion of current target metadata.

There is no legacy fallback, discovery cache, automatic ambiguous-failure retry
or client journal. Connect-family retries retain the same path, body and key.
Default `retry_launch(agent_id)` preserves its legacy one-shot route. Browser,
CLI and MCP launch retry consumers remain separate; no runtime rollout occurs.

Server and native owner: [[gateway/agents/docs/launch-retry.ava.okf.md]].
