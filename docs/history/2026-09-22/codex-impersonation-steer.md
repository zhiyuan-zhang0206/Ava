# Codex impersonation requires Steer delivery

The user requires messages addressed to an impersonated agent to enter the
external controller's active turn. Waiting until that turn finishes can deliver
activation or work after the lease has already ended, so Pending is not an
acceptable substitute for Steer.

The relay therefore fails visibly when Steer is unavailable and leaves the
unacknowledged inbox to Ava's normal handoff. The CLI rejects a request with no
control endpoint before acquiring a lease. We rejected automatic queue fallback:
durable queue acceptance cannot establish timely host receipt.

We retain Codex's atomic start-or-steer operation instead of looking up an active
turn and then calling the explicit steer verb. This avoids a turn-completion race
while preserving the required delivery semantics. Endpoint ownership and actual
processing remain separate: a thread UUID is not a host locator, and only an
explicit controller ACK establishes processing.
