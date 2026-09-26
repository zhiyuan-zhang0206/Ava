---
type: doc
title: Impersonation manifest certification
description: Producer-led delivery certification, trusted controller boundary, and operator evidence.
tags:
- shared
- impersonation
- observability
---

# Impersonation manifest certification

Protocol-v1 admission is off by default and applies only to a new automatic
lease. A host-local, sensitive
`AVA_IMPERSONATION_EVENT_MANIFEST_CERTIFICATION_SECRET` (at least 32
characters) is recorded as a per-lease verifier by the target native host in
its acceptance transaction through a SECURITY DEFINER admission function. The
runner role cannot select that verifier row, rewrite a manifest ledger, or
update `events_completed_at`; certification requires the secret and the fixed
SQL procedure after the final tagged read. This removes the former forgeable
session GUC from the normal runner path.

The proof lives in the unit `.env` for agent-host recovery, but config boot
removes it from every non-finalizer environment before Settings constructs. A
targeted agent-host launch carries a one-use finalizer ticket, consumed before
the file load, so only that process retains the proof. The ticket never joins a
generic child projection; an `execute_code` child, session/daemon child, agent
child, and nested `ava` CLI therefore cannot re-materialize it by booting
config. Same-user direct reads of the unit file remain the documented physical
residual pending the per-machine authority design.

All agent runners currently share the `ava_runner` database role. Native
acceptance binds the proof before its accepted row commits, so a normal request
cannot preempt the target host. A malicious process with the shared runner
credential is nevertheless still a trusted controller: it can exercise
runner-granted lifecycle and ledger surfaces and manufacture an
acceptance/admission sequence before the real host. A host-secret holder can
also certify that host's leases. The gateway/DB owner and runner-host
configuration are trusted. Eliminating this residual requires a design-level
identity change: separate per-machine database principals or a gateway-signed,
mTLS-bound machine capability.

Central audit roots use asserted `agent:<actor>` source, never recipient:
chat/inbound and outbox delivery, spawn, wake, exit, lifecycle, restart/fork,
and computer-daemon actions stage exact bytes in the operation transaction and
emit only after commit. System, user, malformed, and external client sources
stay untagged. The static audit-root census rejects an unclassified constructor.

Each agent-host reconciliation pass monitors incomplete local v1 leases.
Expiry closes admission immediately without waiting for participant seals.
For an ended lease whose manifest is not frozen, replay freezes the union once
all participants have sealed, then performs the same indexed/consumed comparison
as normal release. Open or failed receipts remain pending; a sealed receipt on
a live lease does not close admission. This also repairs a release interrupted
between admission closure and freezing without changing recorded lifecycle facts.
Never-activated, rejected, and non-automatic leases remain untouched: they are
outside both the event-sweep filters (`automatic`, `activated_at`) and the v1
monitor, so no terminal stamp is written for them.
`AVA_IMPERSONATION_EVENT_MANIFEST_SEAL_WAIT_SECONDS` alerts on a still-open
participant but never fails a live SDK `finally`; capture failure and the item
cap are the only capture failures. A lease older than
`AVA_IMPERSONATION_EVENT_DELIVERY_ALERT_AGE_SECONDS` raises
`ImpersonationEventDeliveryPending` without changing state. When its frozen
envelope falls before the Loki retention horizon, it is permanently marked
`retention_loss`, alerts separately, and appears at
`GET /api/alerts/impersonation-event-retention?machine=<runner>` with the
floor, horizon, age inputs, and missing count. Local JSONL evidence never
clears that condition.
Each manifest condition has one alert instance per episode, with `starts_at`
derived from its stored onset. The producer resolves an instance when its
condition clears or a newer onset supersedes it; gateway reconciliation never
touches `machine-probe` rows.

Loki is outside the final SQL transaction. A tagged extra row seen before the
last read refuses certification. One indexed after that read but before (or
after) the completion stamp is an upstream-integrity window: the completed
stamp remains immutable, while the agent-host probes completed leases through
the retained envelope and emits one `ImpersonationEventDeliveryIntegrity`
alert recorded by `event_delivery_integrity_alerted_at`.
