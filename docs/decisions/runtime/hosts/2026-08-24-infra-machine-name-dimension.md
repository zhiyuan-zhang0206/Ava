# Infrastructure metrics use Ava roster identity

## Context

The infrastructure metrics pipeline identified series only by the operating
system hostname. That physical identity is not the deployment identity: the
Windows host reports `zzy-lenovo`, while the Ava roster names its Windows and
WSL units `win` and `wsl`. Grouping on the OS hostname therefore could not
represent those units as separate operational series.

## Decision

Per the 2026-08-24 user ruling, every collector stamps infra datapoints with
both `host` (the OS hostname / physical identity) and `machine_name` (the Ava
roster identity persisted for that unit). Converge bakes `machine_name` into
the collector configuration, and Grafana dashboards and alert rules group by
that label.

The existing `host` label remains because it answers a different question and
can still aid physical-host diagnosis. Reusing the application-metric
`machine` label was rejected: app metrics already carry `machine` and
`agent_id`, while this transform is deliberately confined to the dedicated
infra pipeline. One label keeps one meaning at each telemetry layer.
