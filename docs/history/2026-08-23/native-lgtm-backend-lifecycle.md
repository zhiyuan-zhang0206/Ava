# Native LGTM backend lifecycle

Loki, Prometheus, and Promtail now run as verified native launchd jobs on the
marked LGTM host. Their release assets are pinned by version and SHA256, their
configs and plists converge on every lifecycle run, and their native listeners
remain loopback-only. Tempo and Grafana stay pinned compose services.

The lifecycle keeps the existing host-marker boundary. The watchdog re-runs
the idempotent launcher after a connection failure, but the launcher probes
each native backend first and does not restart a live process. Deliberate
stops remove the marker before teardown.

The former Loki, Prometheus, and Promtail compose volumes remain as rollback
assets. Restoring the prior compose and backend configs from git, then running
`docker compose up -d`, restores the container path without deleting history.

Update: superseded by [native LGTM production alignment](native-lgtm-prod-alignment.md), which records the completed cutover and Promtail retirement.
