# Checkpoint blobs high-water monitoring

## Context

`checkpoint_blobs` accumulates fragmentation/high-water space on an
append-heavy checkpoint workload. Plain autovacuum and the daemon's existing
plain vacuum clear dead rows but do not reclaim the physical file, so the disk
alarm arrives after the table has already grown materially.

## Decision

After every actual existing blob-vacuum pass, record the physical sizes of
`checkpoint_blobs`, `checkpoints`, and `checkpoint_writes` through the existing
application OTLP path. The three values are ObservableGauges, preserving the
most recent measurement between daily window runs. Grafana evaluates the
`checkpoint_blobs` gauge at 2.5 GiB (warning) and 4 GiB (error), both sustained
for two hours.

No scrape endpoint or Prometheus scrape configuration was added: the
events-maintenance daemon already pushes application telemetry through OTLP.
No vacuum setting changed, and the alert only asks an operator to make a
repack/capacity decision before the statvfs disk emergency.

## Consequences

The operator has a table-level warning while reclaiming or provisioning disk
is still a planned action. The latest gauge value is intentionally sampled only
during the 05:00-08:00 vacuum window or an explicit force run, rather than
creating a new polling workload.
