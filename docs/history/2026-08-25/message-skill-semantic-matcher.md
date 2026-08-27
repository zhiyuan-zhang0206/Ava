# Message-to-skill semantic matcher

Ava gained an opt-in semantic complement to the model's manual scan of
`# Capabilities`. On each chat inbound, the matcher can compare the
command-expanded raw text with the live skill index and prepend an advisory
skill-loading note without changing the user's message.

The feature defaults off because each warm-cache enabled turn can spend one
query embedding API call and an uncalibrated fleet-wide rollout could create
noisy hints. Enabled agents use a 300 ms query budget, a maximum of three hints,
and a 0.35 cosine threshold: a deliberately conservative starting point near
the approved 0.3x range that can be tuned per agent from observed precision and
recall.

Skill-document embeddings are a derived per-unit disk cache keyed by the live
skill names, descriptions, identifiers, targets, and `SKILL.md` mtimes. A cold
or stale cache skips the current hint and rebuilds in a daemon thread. Query
timeouts, embedding failures, malformed vectors, and cache failures all omit
the hint rather than delaying or breaking inbound delivery. Descriptions that
trip the Capabilities index's existing security gate never enter the embedding
corpus or a generated hint.
