# Evaluation result separation

Evaluation-isolated agents now receive a gateway-side rejection before any
artifact-read route can reveal another run's transcript, checkpoint, inbound,
activity, timeline, event history, memory search result, or task registry.
The boundary uses the stored isolation flag for an `agent:<id>` caller marker;
unmarked UI and operations requests remain permitted under the existing
peer-trust model.

Each collected evaluation run also records a trace-level leak audit over its
executed code. Reads of memory or result surfaces invalidate that run's score;
attempts to use an SDK surface already disabled for isolated agents remain in
the audit trail without invalidating the score.

This closes the SDK and gateway layers only. Direct filesystem, database, and
raw-network access remain outside this peer-trust boundary and require the
planned OS-level isolation layer.
