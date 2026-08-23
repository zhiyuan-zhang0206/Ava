# Alert ingest reconciliation

Grafana resolve notifications are not durable delivery. If the gateway is
unavailable during the resolution window, Grafana does not retry that edge and
the matching `alerts` row can remain unresolved indefinitely. The ingest side
therefore needs a second path from current evaluator truth to durable alert
state.

The gateway reconciles against Grafana's Alertmanager active-alert API on
startup and every five minutes. The API exposes the exact
`(fingerprint, startsAt)` identity carried by the webhook and stored in Ava, so
the comparison distinguishes separate episodes of the same rule. Rows absent
from a complete snapshot are resolved with an explicit reconciliation note;
direct health and machine probes remain outside the sweep because Grafana does
not own their state.

The comparison excludes rows updated after the snapshot began. Without that
boundary, a firing webhook arriving between the upstream read and the database
update could be resolved against a snapshot that predates it. Upstream,
validation, and database errors all fail closed and leave the store untouched.

A two-hour staleness sweep was rejected. Rule evaluation happens every one or
five minutes, but the notification policy repeats an unchanged firing only
every four hours. Ingest silence at two hours is therefore compatible with a
still-firing alert and cannot safely be treated as resolution truth.
