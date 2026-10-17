---
type: doc
title: Agent contracts and evidence
description: Cross-process agent identity, messages, impersonation, observation and history.
tags: [base]
---

# Agent contracts and evidence

`base/agents/` owns cross-process agent identity, messages, impersonation, observation and history.
Its component nodes describe the current contracts and implementation.
`recovery/` groups shared page-recovery notices and durable automatic-recovery gates;
`base/cluster/authority/event_grants.py` owns runner grants for the impersonation event log.

## Documented components

- [[base/agents/history/hierarchy/docs/hierarchy.meiva.okf.md]] — Hierarchical Understanding.
- [[base/agents/history/docs/message-tokens.meiva.okf.md]] — Per-message true tokens.
- [[base/agents/impersonation/docs/impersonation.meiva.okf.md]] — Named agent impersonation sessions.
- [[base/agents/inbox/docs/inbox.meiva.okf.md]] — Agent inbox.
- [[base/agents/compaction/docs/manual-compact/manual-compact.meiva.okf.md]] — Guarded manual history replacement.
- [[base/agents/incarnation/docs/closed-predecessor.meiva.okf.md]] — Closed predecessor.
- [[base/agents/incarnation/docs/domain-closure.meiva.okf.md]] — Exec domain closure.
- [[base/agents/incarnation/docs/incarnation-resources.meiva.okf.md]] — Incarnation resource evidence.
- [[base/agents/messages/docs/external_caller.meiva.okf.md]] — External Caller Profiles.
- [[base/agents/messages/docs/inbound-provenance.meiva.okf.md]] — Inbound Provenance Facts.
- [[base/agents/messages/docs/kwargs.meiva.okf.md]] — Message Metadata Contract.
- [[base/agents/observation/docs/observation.meiva.okf.md]] — Agent observation evidence.
- [[base/agents/sdk/docs/sampling.meiva.okf.md]] — Live SDK-call sampling and configuration admission.

- [[base/agents/upload_delivery/docs/storage.meiva.okf.md]] — Immutable native upload storage.
