# Unified health schema

## Context

Gateway health, generic daemon health, agent-ops, and events-maintenance each
previously expressed a different definition of healthy. A successful listener
could conceal a stuck operation or sibling maintenance loop, while the watchdog
could only observe an opaque HTTP failure.

## Decision

All health endpoints now use one envelope carrying liveness, readiness,
component progress, and explicit degraded reasons. A non-healthy component
returns HTTP 503, retaining the watchdog's existing consecutive-failure restart
policy as the supervisor.

Events-maintenance reports each loop independently; agent-ops reports the
update lock and oldest active operation, with saturation as informational data.
The gateway reports separate HTTP-serving and database components. Non-2xx
probe details include the envelope's degraded reasons, so watchdog logs identify
the work that requires recovery.

Treating a listener response as proof of all progress was rejected: it makes a
responsive event loop able to conceal a blocked worker. Adding a second restart
controller was also rejected because the existing watchdog already owns restart
thresholds and lifecycle actions.
