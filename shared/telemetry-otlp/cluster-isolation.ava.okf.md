---
type: doc
title: "Telemetry cluster isolation"
description: "The home-derived cluster identity and lgtm-host gates that keep co-located Ava homes from sharing observability by accident."
tags:
- shared
- telemetry
- otlp
- observability
- isolation
---

# Telemetry cluster isolation

## Identity

Every event, metric, and trace Resource carries `cluster`, resolved without the
data plane from `cluster.home_label(ava_home())`, then the home slug, then
`.unknown`. Event JSON and log attributes carry the same value. The designated
LGTM collector drops a record only when its non-null cluster differs from its
own home label; null Resources remain valid for legacy events and the
collector's filelog/infra pipelines.

## Lifecycle and producer gates

`$AVA_HOME/lgtm-host` names the one gateway home that owns the host's LGTM
stack and local collector. A gateway without the marker neither installs that
collector nor exports logs, metrics, or traces to the implicit loopback
endpoint; unified events continue into the local JSONL mirror. An explicit
`AVA_TELEMETRY_OTLP_ENDPOINT` opts the process into a caller-managed collector.
The cached producer verdict warns once per process and applies at restart.

Pure agent-runner homes retain their authenticated relay collector regardless
of the marker: they are transport participants, not competing backend owners.
The collector also omits its Postgres receiver when the direct URL has no
password because otelcol-contrib rejects that configuration; its valid no-auth
Redis receiver remains.

## Read boundary

Gateway Loki reads follow the same ownership rule. An unmarked gateway cannot
use the implicit loopback Loki URL and receives a clean HTTP 503 before any
network request; an explicit `AVA_TELEMETRY_LOKI_URL` is the operator escape
hatch. Dashboard and fleet callers pass their current cluster label, and the
production alert rules filter `cluster=".ava"` after JSON parsing.
That strict filter excludes legacy no-cluster rows for up to Loki retention;
lower aggregate counts are deliberate in exchange for excluding preview pollution.

Parent node: [[telemetry-otlp.ava.okf.md|OTLP export backend & trace ship to Tempo]].
