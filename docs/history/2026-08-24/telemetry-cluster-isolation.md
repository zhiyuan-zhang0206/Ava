# Telemetry cluster isolation

## Context

Multiple Ava homes can share one host, but the observability defaults are
host-loopback endpoints. An unmarked worktree gateway could therefore export
to, or read from, the LGTM stack owned by the marked production home. Port
allocation isolated each cluster's data plane but did not isolate these
host-singleton telemetry ports.

## Decision

The existing `$AVA_HOME/lgtm-host` marker is now the authority for gateway
observability. A gateway without the marker neither starts a local collector
nor exports to or reads from the implicit loopback stack. Explicit OTLP and
Loki endpoint environment variables remain deliberate operator escape hatches.
Pure agent-runners keep their relay behavior because their collector is the
transport to the gateway rather than a competing backend owner.

Isolation is enforced at three independent boundaries:

- Producers attach the home display label as `cluster` to every event, log,
  metric, and trace Resource.
- The marked gateway collector drops non-null Resources whose `cluster` does
  not equal its own label before application signals reach the backends.
  Null-cluster legacy records and collector-owned filelog/infra signals remain
  ingestible during rollout.
- Loki readers, dashboard/fleet aggregates, and alert rules filter the current
  cluster explicitly; an unmarked gateway's implicit Loki read fails cleanly
  with HTTP 503 before any network request.

The cluster label is resolved without touching Postgres or Redis. Resolution
falls back from `home_label` to the home slug and finally `.unknown`, so
observability cannot become a new dependency of telemetry's failure path.

## Alternatives rejected

Deriving ownership from default ports was rejected because the collision is
precisely that several homes share those defaults. `service.name` was rejected
as a cluster key because it describes processes and changes across signals.
Filtering only in Grafana was insufficient: it would leave ingestion, API
reads, and alerts exposed to cross-home data. Requiring `cluster` to be present
at the collector was also rejected for the rollout window because it would
silently discard legacy events and collector-native pipelines.

## Related robustness

The generated collector omits its Postgres receiver when the direct URL has an
empty password, which the contrib receiver rejects, while retaining the valid
unauthenticated Redis receiver. This keeps a no-auth single-box LGTM collector
bootable without weakening the cluster isolation boundary.
