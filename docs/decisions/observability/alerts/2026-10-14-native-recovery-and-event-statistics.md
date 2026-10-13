# Native alert recovery and separate event statistics

## Context

The header alert badge filtered by onset time and folded per-row SSE deltas into
capped pages. Long outages disappeared after 24 hours, and repeated instances
absent from a page inflated its count. Installer ISOs outside /Volumes triggered
all three disk tiers. Root diagnostic rules combined samples across machines and
repeatedly minted episodes after brief recoveries.

The user confirmed that the lower-left Warning / Error card was an event statistic,
requested its earlier compact presentation, and chose to retain existing severity
thresholds and pending periods while reusing native grouping and recovery.

## Decision

The statistics card displayed windowed raw `warnings / errors` event counts,
without an inline class list. Formal alert snapshots remained separate: every
unresolved instance stayed visible, the history window selected recoveries, and
SSE invalidated TanStack Query reads instead of calculating count deltas.

Grafana owned episode recovery through `keepFiringFor: 5m` and matching native
missing-series evaluation budgets. Alertmanager kept its existing per-rule
notification groups and ordinary/critical timing. YAML aliases shared recovery
settings and the writable-filesystem selector in the existing rules catalog.
Root diagnostic queries retained machine identity. Disk rules excluded read-only
and optical-image filesystems through metric metadata.

## Alternatives considered

- Frontend adjacent-time buckets introduced a second episode policy and could
  combine independent failures or disagree with notifications.
- Combining severity tiers into one dynamic rule changed instance identity and
  pending behavior. The user chose to preserve existing escalation timing.
- Excluding the two installer paths addressed one deployment rather than the
  filesystem class that caused the false positive.

## Consequences

Brief recovery retained one native episode; a genuine recovery and later failure
remained distinct episodes. Warning and error tiers could still coexist, by the
user's choice. The badge counted instances, not root causes. API reconciliation
added a read after SSE hints; the server remained authoritative for capped lists,
ordering and counts. Activation required a separately authorized deployment.
