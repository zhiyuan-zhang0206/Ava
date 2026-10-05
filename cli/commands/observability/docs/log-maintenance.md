# Local log maintenance

`ava logs rotate` copytruncates top-level `$AVA_HOME/logs/*.out.log` files and
top-level `$AVA_HOME/lgtm/native/logs/*.log` files when they reach 64 MiB or
their mtime's UTC date differs from today. A zero-byte file never triggers:
there is nothing to archive, and a stale empty log would otherwise produce a
fresh empty archive every day. Grafana logs are excluded because
Grafana rotates itself, as are already dated `*.log.YYYY-MM-DD` archives. The
archive suffix is today's UTC date; an existing archive makes that file a
same-day idempotent no-op. Copytruncate preserves the live path and inode, so a
writer keeps its open file descriptor.

`ava logs retention` removes expired allowlisted files from those two
top-level roots plus the nested computer-use snapshot dir
`$AVA_HOME/logs/computer/snapshots/`. The allowlist covers agent-main
`ava-agent-<id>.out.log`, named PTY
`ava-agent-<id>-shell-<n>-<name>.{out,host}.log`, every service
`ava-*.out.log`, Loguru rotations named
`<service>.YYYY-MM-DD_HH-MM-SS_<pid>.log`, the dated service/native archives
created by `ava logs rotate`, and computer-use snapshots `agent-<id>-<stamp>.png`
(7 days). Rotation stays top-level-only; retention reads the fixed roots above
without general recursion, and neither command follows symlinks; retention also
skips every file held open by a visible process.

Preview the exact paths, UTC mtimes, sizes, and total bytes before deleting:

```bash
ava logs rotate --dry-run
ava logs rotate
ava logs retention --dry-run
ava logs retention
AVA_LOG_RETENTION_DAYS=21 ava logs retention --dry-run
ava logs retention --older-than 21
ava logs retention --family-days agent=15,shell=7,gateway=30,ops=30,watchdog=30,snapshot=7,other=3 --dry-run
```

The age is a positive integer number of days. `--older-than` and
`--family-days` are mutually exclusive. Without either flag, the legacy global
threshold remains: `AVA_LOG_RETENTION_DAYS`, otherwise 14 days. `--older-than`
is the explicit global override. `--family-days` activates the C baseline:
agent-main, `ava-agent-*` service stdout, and their archives 15 days; named PTY
shell transcript/host files and computer-use snapshots 7 days; `gateway*`,
`ops*`, and `*-watchdog` / `*_watchdog` service files and rotations 30 days;
all other service and native
archives 3 days. The rotation shape also admits underscores, so
`delivery_watchdog` is in the watchdog family. Supply only the family values
that differ; omitted values retain that baseline. In a mapping, `default=N`
aliases `other=N` for the catch-all service family.

Dry-run candidates include their family and selected days, followed by one
`retention_family` line per policy family (including zero-candidate families)
with its candidate count, days, and bytes. A file exactly at its cutoff is
retained (`mtime < cutoff` is deleted). Delete failures are reported per path on
stderr, remaining candidates are attempted, and the command exits nonzero if any
inspection or deletion failed.

Converge registers one low-traffic daily job per machine. macOS launchd and
Linux cron run rotation followed by retention at 04:40 local time; the second
command runs only when rotation succeeds. Re-converge replaces the job definitions idempotently, and cluster destroy
removes them.

Raw session output is queried in Loki, not tailed from a file — Grafana Explore
(Loki datasource), `logcli`, or the HTTP API:

```bash
logcli --addr http://127.0.0.1:3100 query '{service_name="ava-gateway"}' --since=1h --limit=100
logcli --addr http://127.0.0.1:3100 query '{service_name=~"ava-agent-.+-shell-.+"}' --tail
curl -G -s http://127.0.0.1:3100/loki/api/v1/query \
  --data-urlencode 'query={service_name="ava-gateway"} |= "error"' \
  --data-urlencode 'limit=50'
```

Raw filelog streams and the OTLP event stream both use `service_name`; filelog
values are session names such as `ava-agent-1818-shell-1` or `ava-gateway`.
Agent loguru JSONL (`agent-{N}.log`) is not scraped — it already reaches Loki
structured via OTLP.

The emitter wiring behind that stream, the unified `events` schema (and its
legacy `agent_events` mirror), and the monthly partitioning are in [the logging owner](../../../../base/log/docs/log.ava.okf.md).
