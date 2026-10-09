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
    idempotency_key="one-deliberate-retry",
    expected_prior_attempt_id=prior,
)
```

Every SDK retry posts to `/api/keyed/v1/agents/{id}/retry-launch` with verified
`principal-v1` scope. The caller key and observed prior UUID are required
arguments. Missing or invalid identities fail before HTTP; there is no opt-in
flag or default one-shot call. `get_launch_attempt` refuses missing or invalid
observations rather than guessing an attempt.

Same-intent recovery reuses the original agent ID, key and observed prior UUID.
It must not refresh the observation before replay: that could change the intent
to a replacement attempt. A deliberately new retry uses a new key and a newly
observed UUID. The existing return type remains the accepted agent ID.
The SDK validates the receipt's agent/prior/new attempt identities and explicit
acceptance/execution flags. Acceptance is historical intent, not runner readiness
or execution evidence, and survives deletion of current target metadata.

There is no legacy fallback, discovery cache, automatic ambiguous-failure retry
or client journal. Connect-family retries retain the same path, body and key.
The project does not retain SDK compatibility for the former single-argument
call or admission flag. Browser HTTP entry cleanup is tracked separately in
#4473; old installed consumers are not an adoption or completion gate. No
runtime rollout occurs.

Server and native owner: [[gateway/agents/docs/launch-retry.ava.okf.md]].
