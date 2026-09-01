# Recall observability and explicit task metering

Task #5643 keeps recall quality observation separate from evaluation. Recall
filter verdicts record a SHA-256 digest of the query and a bounded sample of
the injected paths, never the query text. Existing `passive_recall` timing
fields are surfaced as search and filter latency panels in Plugin quality.

Task budgets use explicit attribution rather than an owner's lifetime usage:
a task-associated system note carries its id through the agent turn, and only
that turn's `llm_usage` event and cumulative task counters receive the id.
Chat and unassociated inbound work clear it, so agents that own multiple tasks
cannot spend one task's budget on another. The token and USD ceilings notify
the task owner once after they are reached; they do not terminate a running
agent. Bare `python x.py` children remain deliberately unmetered and cannot
contribute to a task budget.

No evaluation harness or recall evaluation data was introduced.

Update: model-originated paths, malformed replies, and provider errors are not
logged by recall filtering, so the digest is the only query-derived telemetry.
Distinct task notes co-claimed into one turn deliberately leave that turn
untagged; a tagged note must name an existing task owned by its recipient.
