"""`ava trace ship` — replay the local trace mirror (collector JSONL) to Tempo.

Recording (shared/trace.py) exports spans over OTLP/HTTP to the local OTel
Collector sidecar, whose file exporter mirrors them to `$AVA_HOME/traces/`:
the active `spans.jsonl` plus rotated `spans-<ISO-timestamp>.jsonl` backups,
each line a standard OTLP/JSON `ExportTraceServiceRequest`. This command is
the RECOVERY consumer: it reads the mirror and POSTs each line back over
OTLP/HTTP (protobuf) straight to Tempo's OTLP endpoint
(`AVA_TELEMETRY_TEMPO_ENDPOINT`, default http://127.0.0.1:14318 on the LGTM
host):

- **why bypass the sidecar**: the live fan-out is the sidecar's job, with a
  persistent file-backed queue for backend outages. Ship exists for the gaps
  the queue could not hold (backend down longer than the queue, offline
  machines, re-import of a past window). Replaying through the sidecar would
  write the replayed lines back into the mirror (the file exporter mirrors
  everything it receives), looping the watermark — so ship dials Tempo
  directly.
- **gating**: refuses while `AVA_TELEMETRY_OTLP_ENABLED=false` (one kill
  switch for the whole OTLP surface — with the sidecar architecture that also
  stops recording, so there is nothing to replay either).

Two modes:
- **incremental** (default): a per-file byte-offset watermark
  (`traces/.ship-watermark.json`) records how far each file has been shipped, so
  re-running ships only new lines.
- **windowed** (`--since` / `--until`, YYYY-MM-DD): ship whole files whose day
  stamp (name stamp, or mtime for the unstamped active file) falls in the
  range, ignoring the watermark. Span ingestion is idempotent by span id, so
  re-shipping an already-shipped window is safe.

Legacy `spans-YYYYMMDD-<pid>.jsonl` files (the pre-#1266 agent-side mirror)
are still read — a host upgraded mid-window replays its old files too.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from shared.config import settings
from shared.paths import traces_dir

_WATERMARK_NAME = ".ship-watermark.json"
# The standard OTLP/HTTP trace path — what Tempo's OTLP receiver listens on.
_OTLP_V1_PATH = "/v1/traces"


class TraceShipError(RuntimeError):
    """Shipping config is incomplete — AVA_TELEMETRY_OTLP_ENABLED is false."""


def _require_ship_config() -> str:
    """Validate the consumer config, returning the OTLP traces endpoint.

    Raises:
        TraceShipError: AVA_TELEMETRY_OTLP_ENABLED=false (the whole-OTLP kill
            switch).
    """
    if not settings.observability.telemetry_otlp_enabled:
        raise TraceShipError(
            "AVA_TELEMETRY_OTLP_ENABLED is false — the whole OTLP surface "
            "(live exporter + trace ship) is off. Set it true to ship the "
            "mirror to Tempo."
        )
    return settings.observability.telemetry_tempo_endpoint.rstrip("/") + _OTLP_V1_PATH


def _file_day(path: Path) -> date:
    """Day stamp of a mirror file: legacy `spans-YYYYMMDD-<pid>.jsonl` names,
    the collector's rotated `spans-<ISO-timestamp>.jsonl` names, else the
    file's mtime (the active `spans.jsonl` carries no stamp — its content is
    today's)."""
    from shared.trace import _mirror_day

    day = _mirror_day(path)
    if day is not None:
        return day
    # The active file carries no stamp; its day is the mtime in UTC — the same
    # clock the legacy day stamps and --since/--until use.
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()


def _load_watermark() -> dict[str, int]:
    path = traces_dir() / _WATERMARK_NAME
    if not path.exists():
        return {}
    marks: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
    # Drop entries for files retention-pruned out of the mirror, so the watermark
    # does not grow unbounded as per-pid daily files cycle.
    return {name: off for name, off in marks.items() if (traces_dir() / name).exists()}


def _save_watermark(marks: dict[str, int]) -> None:
    path = traces_dir() / _WATERMARK_NAME
    path.write_text(json.dumps(marks, indent=2, sort_keys=True), encoding="utf-8")


def _post_line(client: httpx.Client, endpoint: str, line: str) -> int:
    """POST one OTLP/JSON line as protobuf; return the span count in it.

    Reconstructs the OTLP `ExportTraceServiceRequest` from the stored JSON and
    re-serializes to protobuf — the exact wire shape Tempo's OTLP endpoint
    ingests.
    """
    from google.protobuf.json_format import Parse
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    request = Parse(line, ExportTraceServiceRequest())
    body = request.SerializeToString()
    headers = {"Content-Type": "application/x-protobuf"}
    resp = client.post(endpoint, content=body, headers=headers)
    resp.raise_for_status()
    return sum(len(ss.spans) for rs in request.resource_spans for ss in rs.scope_spans)


def _ship_files(
    files: list[Path],
    marks: dict[str, int],
    *,
    endpoint: str | None,
    windowed: bool,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Ship one file list; return (total_lines, total_spans, shipped_files).

    Incremental mode starts each file at its watermark offset and advances the
    watermark per line so a crash never re-sends or skips. Windowed mode ships
    files whole (re-imports are idempotent, so the watermark is untouched).
    Binary mode: seek/offset are true byte positions, so an interrupted ship
    resumes exactly after the last POSTed line (text-mode tell() is an opaque
    cookie and is disabled mid-iteration).
    """
    total_lines = 0
    total_spans = 0
    shipped_files = 0
    with httpx.Client(timeout=30.0) as client:
        for path in files:
            start = 0 if windowed else marks.get(path.name, 0)
            if start >= path.stat().st_size:
                continue
            with path.open("rb") as f:
                f.seek(start)
                offset = start
                file_lines = 0
                file_spans = 0
                for raw in f:
                    offset += len(raw)
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    if dry_run:
                        file_lines += 1
                        continue
                    if endpoint is None:  # pragma: no cover — unreachable
                        # _require_ship_config ran above when not dry_run; keep the
                        # narrowing honest instead of casting None to str.
                        raise TraceShipError("ship config required for a real ship")
                    file_spans += _post_line(client, endpoint, line)
                    file_lines += 1
                    # Advance the watermark per line so a crash never re-sends or
                    # skips. Windowed re-imports are idempotent, so skip the bump.
                    if not windowed:
                        marks[path.name] = offset
                        _save_watermark(marks)
            if file_lines:
                shipped_files += 1
                total_lines += file_lines
                total_spans += file_spans
                print(f"  {path.name}: {file_lines} batches, {file_spans} spans")
    return total_lines, total_spans, shipped_files


def cmd_trace_ship(*, since: str | None, until: str | None, dry_run: bool) -> int:
    """Ship the local trace mirror to Tempo (the only viewer backend).

    With no --since/--until, ships incrementally from the per-file watermark.
    With a window, ships matching files whole (watermark untouched).

    ``dry_run`` reads the mirror only and needs no backend config — it is the
    one way to inspect what is recorded when the backend is off (the mirror is
    the durable record either way).
    """
    endpoint = _require_ship_config() if not dry_run else None
    windowed = since is not None or until is not None
    lo = datetime.strptime(since, "%Y-%m-%d").date() if since else date.min  # noqa: DTZ007 — date-only
    hi = datetime.strptime(until, "%Y-%m-%d").date() if until else date.max  # noqa: DTZ007 — date-only

    from shared.trace import _mirror_sort_key

    # Active `spans.jsonl` + rotated `spans-<ts>.jsonl` + legacy
    # `spans-YYYYMMDD-<pid>.jsonl`; oldest first, the active file last.
    files = sorted(traces_dir().glob("spans*.jsonl"), key=_mirror_sort_key)
    if windowed:
        files = [p for p in files if lo <= _file_day(p) <= hi]

    marks = _load_watermark()
    total_lines, total_spans, shipped_files = _ship_files(
        files, marks, endpoint=endpoint, windowed=windowed, dry_run=dry_run
    )

    verb = "would ship" if dry_run else "shipped"
    scope = f"window {since or '-inf'}..{until or '+inf'}" if windowed else "incremental"
    print(f"{verb} ({scope}): {shipped_files} files, {total_lines} batches, {total_spans} spans")
    if dry_run:
        print("target: (dry-run — would ship to tempo, no backend config required)")
    else:
        print(f"target: tempo -> {endpoint}")
    return 0
