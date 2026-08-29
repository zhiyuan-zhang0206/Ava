#!/usr/bin/env python3
"""Fetch one OTel trace as normalized JSON — from the local mirror or Tempo.

Two modes:

- **Find** (`--search <traceql>`): query Tempo's search API and list matching
  traces (id, duration, matched span count). Run this first to get trace ids.
- **Fetch** (`--trace-id <hex-32>`): pull one trace's full span set and write
  `trace_raw.json` for `read_trace.py`. Sources:
    - `mirror` (default): scan `$AVA_HOME/traces/spans*.jsonl*` (active `spans.jsonl` + rotated `spans-<ISO>(-size|-time)?.jsonl`, gzipped old segments read transparently) — the durable
      record, complete, no size cap, no network. Needs filesystem access to
      the machine that recorded the trace. Every file in range is scanned
      and spans are merged (a trace can straddle the sidecar's rotation
      boundary). Ids are hex in the mirror (the collector's file exporter
      writes 32/16-char hex, not OTLP-JSON base64); legacy pre-#1266
      agent-side mirror files carry base64 and are handled too.
    - `tempo`: `GET /api/traces/{id}` — refused above Tempo's 5 MB cap
      ("trace exceeds max size"); the error message points back to `mirror`.

Output `trace_raw.json` (contract for read_trace.py):

    {
      "trace_id": "<hex-32>",
      "source": "mirror" | "tempo",
      "fetched_at": "<ISO-8601>",
      "spans": [
        {
          "span_id": "<hex-16>",
          "parent_span_id": "<hex-16>" | null,
          "name": str,
          "kind": "INTERNAL" | "CLIENT" | "SERVER" | "PRODUCER" | "CONSUMER" | null,
          "start_ns": int, "end_ns": int,
          "status": "OK" | "ERROR" | null,
          "attributes": {key: scalar}
        }
      ]
    }

stdlib only; runs inside the repo venv or any Python 3.12.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

_KIND_STRIP = "SPAN_KIND_"
_STATUS_OK = "STATUS_CODE_OK"
_STATUS_ERROR = "STATUS_CODE_ERROR"


def _anyvalue(value: dict) -> object:
    """Collapse a proto3-JSON AnyValue dict to a plain scalar/list."""
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        return [_anyvalue(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            f["key"]: _anyvalue(f["value"])
            for f in value["kvlistValue"].get("values", [])
            if "key" in f and "value" in f
        }
    return None


def _id_to_hex(value: str) -> str:
    """Normalize a trace/span id field to lowercase hex.

    The mirror (collector file exporter, task #1266) and Tempo both carry ids
    as hex strings today; legacy OTLP/JSON envelopes (pre-#1266 agent-side
    mirror) carry base64 per the OTLP JSON spec. Detect by shape: hex ids are
    32 (trace) or 16 (span) chars; base64 ids are 24 or 12. Anything else is
    malformed and refused rather than decoded into garbage.
    """
    if len(value) in (32, 16) and re.fullmatch(r"[0-9a-fA-F]+", value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception:
        # Unpadded base64 (legacy OTLP JSON writers may omit the trailing
        # "="); pad to a multiple of 4 and retry the strict decode.
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
        except Exception as exc:  # binascii.Error / ValueError
            raise ValueError(f"malformed span id {value[:24]!r} (neither hex nor base64)") from exc
    if len(decoded) not in (16, 8):
        raise ValueError(
            f"malformed span id {value[:24]!r}: decoded to {len(decoded)} bytes, "
            "expected 16 (trace) or 8 (span)"
        )
    return decoded.hex()


def _normalize_span(sp: dict) -> dict:
    """One OTLP span dict (proto3 JSON, hex or base64 ids) -> normalized dict."""
    attributes = {a["key"]: _anyvalue(a["value"]) for a in sp.get("attributes", [])}
    kind = sp.get("kind")
    if isinstance(kind, str):
        kind = kind.removeprefix(_KIND_STRIP) or None
    status = sp.get("status", {})
    code = status.get("code", "") if status else ""
    if isinstance(code, str):
        status_out = "ERROR" if code == _STATUS_ERROR else ("OK" if code == _STATUS_OK else None)
    elif isinstance(code, int):
        status_out = "ERROR" if code == 2 else ("OK" if code == 1 else None)
    else:
        status_out = None
    return {
        "span_id": _id_to_hex(sp["spanId"]),
        "parent_span_id": _id_to_hex(sp["parentSpanId"]) if sp.get("parentSpanId") else None,
        "name": sp.get("name", ""),
        "kind": kind,
        "start_ns": int(sp["startTimeUnixNano"]),
        "end_ns": int(sp["endTimeUnixNano"]),
        "status": status_out,
        "attributes": attributes,
    }


def _spans_from_envelope(data: dict, hex_trace_id: str | None) -> list[dict]:
    """Walk an OTLP JSON envelope (mirror line or Tempo batches) for spans."""
    # Mirror line: {"resourceSpans": [...]} — Tempo full trace: {"batches": [...]}.
    groups = data.get("resourceSpans") or data.get("batches") or []
    out: list[dict] = []
    for group in groups:
        for ss in group.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                try:
                    if hex_trace_id is not None and _id_to_hex(sp["traceId"]) != hex_trace_id:
                        continue
                    out.append(_normalize_span(sp))
                except (KeyError, ValueError):
                    # A malformed sibling span (empty traceId, missing field,
                    # garbage id) must never abort the whole scan — skip it
                    # and keep going (the pre-#637 behavior).
                    continue
    return out


# ── find mode ────────────────────────────────────────────────────────────────


def _http_get_json(url: str) -> dict:
    import urllib.request

    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-http(s) URL: {url[:60]!r}")
    # Bypass the system HTTP proxy: cluster endpoints are loopback or on the
    # private cluster network, and the macOS system proxy (Clash/VPN on
    # 127.0.0.1:7897) answers 502 for them. Same rationale as the
    # `trust_env=False` in cli/commands/trace.py's ship path.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as resp:
        return json.loads(resp.read().decode())


def cmd_search(args) -> int:
    import urllib.parse

    params = {"q": args.search, "limit": str(args.limit)}
    if args.spss:
        params["spss"] = str(args.spss)
    url = args.tempo_url.rstrip("/") + "/api/search?" + urllib.parse.urlencode(params)
    data = _http_get_json(url)
    traces = data.get("traces", [])
    print(f"{len(traces)} trace(s) match {args.search!r} ({args.tempo_url})")
    for t in traces:
        start_ns = int(t.get("startTimeUnixNano", 0))
        started = datetime.fromtimestamp(start_ns / 1e9, tz=UTC).isoformat()
        # Tempo search trims leading zero nibbles; pad back so the id pastes
        # straight into --trace-id. Lowercase too: the ids are hex, and
        # cmd_fetch accepts [0-9a-f] only.
        trace_id = t["traceID"].lower().zfill(32)
        print(
            f"  {trace_id}  dur={t.get('durationMs')}ms  "
            f"spans={t.get('spanSet', {}).get('matched')}  root={t.get('rootServiceName')}  "
            f"start={started}"
        )
    return 0


# ── fetch mode ────────────────────────────────────────────────────────────────


def _mirror_dir(args) -> Path:
    if args.mirror_dir:
        return Path(args.mirror_dir)
    ava_home = Path(os.environ.get("AVA_HOME", Path.home() / ".ava"))
    return ava_home / "traces"


def _trace_id_b64(hex_trace_id: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_trace_id)).decode()


def _mirror_day(fp: Path) -> datetime.date | None:
    """Day stamp from a mirror filename (legacy `spans-YYYYMMDD-<pid>.jsonl`
    or the sidecar's rotated `spans-<ISO-timestamp>.jsonl`); None for the
    unstamped ACTIVE `spans.jsonl`."""
    m = re.match(r"spans-(\d{8})-", fp.name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=UTC).date()
        except ValueError:
            return None
    m = re.match(r"spans-(\d{4})-(\d{2})-(\d{2})T", fp.name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _mirror_files(directory: Path, days: int) -> list[Path]:
    """Mirror files within the last `days` days, newest first: the active
    `spans.jsonl` (day = mtime) plus rotated/legacy `spans-*.jsonl` and
    gzipped old segments (`*.jsonl.gz`)."""
    cutoff = datetime.now(UTC).date() - timedelta(days=days - 1)
    files = sorted(directory.glob("spans*.jsonl*"), reverse=True)
    kept = []
    for fp in files:
        day = _mirror_day(fp)
        if day is None:
            day = datetime.fromtimestamp(fp.stat().st_mtime, tz=UTC).date()
        if day >= cutoff:
            kept.append(fp)
        elif kept:
            break  # files are newest-first; the rest are older
    return kept


def fetch_from_mirror(args, hex_trace_id: str) -> list[dict]:
    directory = _mirror_dir(args)
    # Current mirror lines carry hex ids; legacy pre-#1266 lines carry
    # base64 (protojson, padded, but unpadded writers exist) — include the
    # unpadded form too, or the prefilter would drop those lines before
    # `_id_to_hex`'s padding fallback ever runs.
    b64_id = _trace_id_b64(hex_trace_id)
    needles = {hex_trace_id, b64_id, b64_id.rstrip("=")}
    files = _mirror_files(directory, args.days)
    print(f"scanning {len(files)} mirror file(s) in {directory} (days={args.days})")
    # A trace can straddle a rotation boundary (the sidecar rotates on size),
    # so scan every file in range and merge — never stop at the first hit.
    by_span: dict[str, dict] = {}
    for fp in files:
        found = 0
        # Old segments are gzipped by the agent-side compression pass; read
        # them transparently. `fp.open` is a bound method and must NOT be
        # called with the path as its first argument (gzip.open does take
        # it) — passing it crashed every non-gzipped mirror scan.
        with (
            gzip.open(fp, "rt", encoding="utf-8")
            if fp.name.endswith(".gz")
            else fp.open("rt", encoding="utf-8")
        ) as f:
            for line in f:
                if not any(needle in line for needle in needles):
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for span in _spans_from_envelope(req, hex_trace_id):
                    by_span[span["span_id"]] = span
                    found += 1
        if found:
            print(f"found {found} span(s) in {fp.name}")
    return list(by_span.values())


def fetch_from_tempo(args, hex_trace_id: str) -> list[dict]:
    import urllib.error

    url = f"{args.tempo_url.rstrip('/')}/api/traces/{hex_trace_id}"
    try:
        data = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        if "exceeds max size" in body:
            print(
                "tempo: trace exceeds the 5 MB full-trace cap. Use "
                "`--source mirror` on the recording machine, or narrow with --search."
            )
            return []
        raise
    spans = _spans_from_envelope(data, hex_trace_id)
    print(f"tempo: {len(spans)} spans from {url}")
    return spans


def cmd_fetch(args) -> int:
    if not args.trace_id:
        print("--trace-id is required in fetch mode")
        return 2
    if not re.fullmatch(r"[0-9a-f]{31,32}", args.trace_id):
        print("--trace-id must be a 31- or 32-char hex id")
        return 2
    trace_id = args.trace_id.zfill(32)
    spans = (
        fetch_from_mirror(args, trace_id)
        if args.source == "mirror"
        else fetch_from_tempo(args, trace_id)
    )
    if not spans:
        print(
            "no spans found. Is the trace id right? Old mirror files are "
            "retention-pruned — widen --days. In Tempo, traces arrive via "
            "`ava trace ship` (gap-replay; the sidecar fans out live)."
        )
        return 1
    spans.sort(key=lambda s: s["start_ns"])
    out = {
        "trace_id": trace_id,
        "source": args.source,
        "fetched_at": datetime.now(UTC).isoformat(),
        "spans": spans,
    }
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {len(spans)} spans -> {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[1])
    ap.add_argument(
        "--tempo-url",
        default=os.environ.get(
            "AVA_TRACE_TEMPO_URL",
            os.environ.get("AVA_TELEMETRY_TEMPO_QUERY_URL", "http://localhost:3200"),
        ),
        help=(
            "Tempo query API base (or the Grafana datasource proxy path). "
            "Default: AVA_TRACE_TEMPO_URL, else AVA_TELEMETRY_TEMPO_QUERY_URL "
            "(the per-cluster Tempo query URL; remote when Tempo is not on "
            "this host), else http://localhost:3200."
        ),
    )
    ap.add_argument("--out", default="trace_raw.json")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--search", metavar="TRACEQL", help="find traces (Tempo search API)")
    mode.add_argument("--trace-id", metavar="HEX32", help="fetch one full trace")
    ap.add_argument("--limit", type=int, default=10, help="search mode: max traces")
    ap.add_argument("--spss", type=int, default=3, help="search mode: spans per span set")
    ap.add_argument(
        "--source",
        choices=("mirror", "tempo"),
        default="mirror",
        help="fetch mode: mirror = $AVA_HOME/traces JSONL (default); tempo = /api/traces/{id}",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=2,
        help="fetch mode (mirror): scan files from the last N days, newest first",
    )
    ap.add_argument(
        "--mirror-dir",
        default=None,
        help="fetch mode (mirror): override $AVA_HOME/traces",
    )
    args = ap.parse_args()
    return cmd_search(args) if args.search else cmd_fetch(args)


if __name__ == "__main__":
    raise SystemExit(main())
