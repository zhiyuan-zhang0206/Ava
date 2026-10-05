# Observability operations

Current delivery, retention and failure contracts belong to their component owners:

- [Native LGTM lifecycle](lgtm.ava.okf.md)
  and [backend configuration](../../../../deploy/lgtm/README.md).
- [OTLP producer export](../../../../base/telemetry/otlp/docs/telemetry-otlp/telemetry-otlp.ava.okf.md),
  [delivery topology and recovery mirror](../../../../base/telemetry/otlp/docs/telemetry-otlp/trace-mirror.ava.okf.md),
  [bounded queues and loss](../../../../base/telemetry/otlp/docs/telemetry-otlp/export-backpressure.ava.okf.md).
- [Infrastructure metrics](../../../../base/telemetry/otlp/docs/telemetry-otlp/infra-metrics.ava.okf.md)
  and [event labels](../../../../base/telemetry/otlp/docs/telemetry-otlp/event-resource-labels.ava.okf.md).

## Change backend selection

On the observability station, `ava lgtm status` and `ava status` report root-owned
readiness. Configuration or service-selection changes require a normal stop
before start; stored observation data survives. `ava lgtm on` and `ava lgtm off`
change only the three backend selections and preserve the other service choices.
Inspect the rendered configuration and pinned versions through the backend owner
above. Disabling the backend also removes the live data source for its consumers;
check those consumers before choosing an outage window.

`AVA_TELEMETRY_OTLP_ENABLED=false` disables producer export and trace shipping;
changing this startup-applied setting requires restarting the affected processes.
The collector's own infrastructure scrapes have an independent role gate. To
stop that service, use `ava start --disable-service otel-collector` through normal
service-selection lifecycle, rather than treating the producer switch as a
collector stop.

## Verify indexed event labels

**Event-label canary.** After an OTLP-emitter rollout, query a post-rollout
window and require every event stream label to equal its JSON event name. This
checks the indexed read path, not merely content filtering:

```bash
.venv/bin/python - <<'PY'
from datetime import UTC, datetime, timedelta
import json
from urllib.parse import urlencode
from urllib.request import urlopen

end = datetime.now(UTC)
params = urlencode(
    {
        "query": '{service_name="unknown_service"}',
        "start": str(int((end - timedelta(minutes=15)).timestamp() * 1_000_000_000)),
        "end": str(int(end.timestamp() * 1_000_000_000)),
        "limit": "2000",
    }
)
with urlopen(f"http://127.0.0.1:3100/loki/api/v1/query_range?{params}") as response:  # noqa: S310 — loopback Loki canary
    result = json.load(response)["data"]["result"]

mismatches = [
    (stream["stream"].get("event_name"), json.loads(line).get("event_name"))
    for stream in result
    for _, line in stream["values"]
    if stream["stream"].get("event_name") != json.loads(line).get("event_name")
]
observed_rows = sum(len(stream["values"]) for stream in result)
assert observed_rows >= 1, "No event rows observed; an empty sample cannot certify indexed labels"
assert not mismatches, mismatches[:20]
print(f"checked {observed_rows} event rows; no label mismatches")
PY
```

An empty sample cannot certify labels and fails the canary. An absent
`event_name` label or any mismatch also fails; run it only over
newly emitted rows, since indexed-era data from before the rollout is immutable.

## Replay a trace mirror

**Ship** — `ava trace ship` (`cli/commands/observability/trace.py`). Recovery replay reads
the mirror and bypasses the LOCAL sidecar, because replaying through it would
write the replayed lines back into the mirror (watermark loop). A gateway or
single-box unit POSTs straight to loopback
`{AVA_TELEMETRY_TEMPO_ENDPOINT}/v1/traces` without auth; a pure runner POSTs
to the gateway collector's configured private OTLP port with its telemetry token. The
remote trace pipeline writes Tempo only and never the gateway mirror, avoiding
a second copy and replay ambiguity. Needed only for gaps the queue could not
hold (backend down longer than the queue, offline machines, past windows).
Gated by `AVA_TELEMETRY_OTLP_ENABLED` (refuses while off — one kill switch
for the whole OTLP surface).
- **incremental** (no args): a per-file byte-offset watermark
  (`traces/.ship-watermark.json`) advances per POSTed line, so re-running ships
  only new lines and an interrupted ship resumes exactly where it stopped.
- **windowed** (`--since` / `--until`, `YYYY-MM-DD`): ships matching files whole,
  ignoring the watermark — the "shipping was off, import a past range" path. Span
  ingestion is idempotent by span id, so re-shipping is safe.


For bounded trace investigation, use [inspect-a-trace](../../../../.agents/skills/inspect-a-trace/SKILL.md).
