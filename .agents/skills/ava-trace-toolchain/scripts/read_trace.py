#!/usr/bin/env python3
"""Read a fetched trace: span tree, LLM calls, node sequence, turn content.

Input: `trace_raw.json` (from fetch_trace.py). Output: `trace_read.json` plus
a text summary on stdout.

    {
      "trace_id": "<hex-32>",
      "source": "mirror" | "tempo",
      "agent_id": "<session.id>" | null,
      "checkpoint_id": "<ava.checkpoint_id>" | null,
      "workflow_name": str | null,
      "span_count": int, "duration_ms": int,
      "started_at": "<ISO-8601>",
      "tree": [ {span_id, name, kind, depth, start_offset_ms, duration_ms,
                 status, model, node, children: [...]} ],
      "llm_spans": [ {span_id, name, model, start_offset_ms, duration_ms, status} ],
      "node_sequence": [ {step, node, span_id, start_offset_ms, duration_ms} ],
      "content": {pruned, messages: [...]} | null,
      "events": [EventRow...] | null
    }

`--with-content` / `--with-events` join gateway data by trace id:

- content: `GET /api/agents/{agent}/traces/{trace_id}/messages` — the turn's
  complete message history from the checkpoint. `pruned: true` = checkpoint
  trimmed (expected for old turns). 404 = agent no longer exists.
- events: `GET /api/events?trace_id=...` — the correlated event stream.

Gateway auth: `Authorization: Bearer <secret>` where the secret is
`AVA_CLUSTER_SECRET` from the environment or `$AVA_HOME/.env` (no header when
empty — a single-box no-auth cluster). The gateway listens on :8000.

LLM span detection: span name ends with `.chat`, or attribute
`gen_ai.operation.name` == "chat". Model comes from
`traceloop.association.properties.ls_model_name`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

_AGENT_ATTR = "session.id"
_CHECKPOINT_ATTR = "ava.checkpoint_id"
_WORKFLOW_ATTR = "traceloop.workflow.name"
_MODEL_ATTR = "traceloop.association.properties.ls_model_name"
_NODE_ATTR = "traceloop.association.properties.langgraph_node"
_STEP_ATTR = "traceloop.association.properties.langgraph_step"
_OP_ATTR = "gen_ai.operation.name"


def _load_raw(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "spans" not in data:
        raise SystemExit(f"{path} is not a trace_raw.json (no 'spans')")
    return data


def _root_meta(spans: list[dict]) -> dict:
    """Root span (no parent) attributes → agent / checkpoint / workflow."""
    workflow = next(
        (
            sp["attributes"].get(_WORKFLOW_ATTR)
            for sp in spans
            if sp["attributes"].get(_WORKFLOW_ATTR)
        ),
        None,
    )
    for sp in spans:
        if sp["parent_span_id"] is None:
            attrs = sp["attributes"]
            return {
                "agent_id": str(attrs.get(_AGENT_ATTR))
                if attrs.get(_AGENT_ATTR) is not None
                else None,
                "checkpoint_id": attrs.get(_CHECKPOINT_ATTR),
                "workflow_name": workflow,
                "root_span_id": sp["span_id"],
                "root_name": sp["name"],
            }
    return {
        "agent_id": None,
        "checkpoint_id": None,
        "workflow_name": None,
        "root_span_id": None,
        "root_name": None,
    }


def _is_llm(sp: dict) -> bool:
    if sp["name"].endswith(".chat"):
        return True
    return sp["attributes"].get(_OP_ATTR) == "chat"


def _build_tree(spans: list[dict], start_ns: int) -> list[dict]:
    children: dict[str | None, list[dict]] = {}
    for sp in spans:
        children.setdefault(sp["parent_span_id"], []).append(sp)
    for lst in children.values():
        lst.sort(key=lambda s: s["start_ns"])

    def walk(parent: str | None, depth: int) -> list[dict]:
        out = []
        for sp in children.get(parent, []):
            attrs = sp["attributes"]
            out.append(
                {
                    "span_id": sp["span_id"],
                    "name": sp["name"],
                    "kind": sp["kind"],
                    "depth": depth,
                    "start_offset_ms": round((sp["start_ns"] - start_ns) / 1e6, 2),
                    "duration_ms": round((sp["end_ns"] - sp["start_ns"]) / 1e6, 2),
                    "status": sp["status"],
                    "model": attrs.get(_MODEL_ATTR),
                    "node": attrs.get(_NODE_ATTR),
                    "children": walk(sp["span_id"], depth + 1),
                }
            )
        return out

    return walk(None, 0)


def _node_sequence(spans: list[dict], start_ns: int) -> list[dict]:
    nodes: dict[str, dict] = {}
    for sp in spans:
        node = sp["attributes"].get(_NODE_ATTR)
        if node is None:
            continue
        step = sp["attributes"].get(_STEP_ATTR)
        if node not in nodes or sp["start_ns"] < nodes[node]["_first_ns"]:
            nodes[node] = {
                "node": node,
                "step": step,
                "span_id": sp["span_id"],
                "_first_ns": sp["start_ns"],
                "start_offset_ms": round((sp["start_ns"] - start_ns) / 1e6, 2),
                "duration_ms": round((sp["end_ns"] - sp["start_ns"]) / 1e6, 2),
            }
    seq = sorted(nodes.values(), key=lambda n: n["_first_ns"])
    for n in seq:
        n.pop("_first_ns", None)
    return seq


# ── gateway joins ─────────────────────────────────────────────────────────────


def _cluster_secret() -> str:
    env = os.environ.get("AVA_CLUSTER_SECRET")
    if env is not None:
        return env
    ava_home = Path(os.environ.get("AVA_HOME", Path.home() / ".ava"))
    env_file = ava_home / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        m = re.match(r"AVA_CLUSTER_SECRET=(.+)", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def _gateway_get(gateway: str, path: str) -> dict:
    import urllib.request

    url = gateway.rstrip("/") + path
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-http(s) gateway URL: {url[:60]!r}")
    headers = {}
    secret = _cluster_secret()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _fetch_content(gateway: str, agent_id: str, trace_id: str) -> dict:
    return _gateway_get(gateway, f"/api/agents/{agent_id}/traces/{trace_id}/messages")


def _fetch_events(gateway: str, trace_id: str, from_iso: str) -> dict:
    import urllib.parse

    params = urllib.parse.urlencode({"trace_id": trace_id, "from": from_iso, "limit": "1000"})
    return _gateway_get(gateway, f"/api/events?{params}")


def _print_summary(out: dict) -> None:
    print(f"trace {out['trace_id']}  source={out['source']}")
    print(
        f"agent={out['agent_id']}  workflow={out['workflow_name']}  "
        f"checkpoint={out['checkpoint_id']}"
    )
    print(
        f"spans={out['span_count']}  duration={out['duration_ms']}ms  "
        f"llm_calls={len(out['llm_spans'])}  nodes={len(out['node_sequence'])}  "
        f"started={out['started_at']}"
    )
    print("node sequence:", " -> ".join(n["node"] for n in out["node_sequence"]))
    for llm in out["llm_spans"][:10]:
        print(
            f"  LLM {llm['name']}  model={llm['model']}  "
            f"+{llm['start_offset_ms']}ms  {llm['duration_ms']}ms  {llm['status']}"
        )
    if len(out["llm_spans"]) > 10:
        print(f"  ... and {len(out['llm_spans']) - 10} more LLM spans (see the JSON)")
    if out["content"] is not None:
        print(
            f"content: pruned={out['content']['pruned']}  "
            f"messages={len(out['content'].get('messages', []))}"
        )
    if out["events"] is not None:
        print(f"events: {len(out['events'])} rows")


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[1])
    ap.add_argument("raw", help="trace_raw.json from fetch_trace.py")
    ap.add_argument("--out", default="trace_read.json")
    ap.add_argument(
        "--with-content",
        action="store_true",
        help="join the turn's full messages via the gateway trace endpoint",
    )
    ap.add_argument(
        "--with-events",
        action="store_true",
        help="join the correlated event stream via /api/events",
    )
    ap.add_argument(
        "--gateway",
        default=os.environ.get("AVA_GATEWAY_URL", "http://localhost:8000"),
        help="gateway base URL for content/events joins",
    )
    args = ap.parse_args()

    raw = _load_raw(Path(args.raw))
    spans = raw["spans"]
    start_ns = min(sp["start_ns"] for sp in spans)
    end_ns = max(sp["end_ns"] for sp in spans)
    meta = _root_meta(spans)

    out = {
        "trace_id": raw["trace_id"],
        "source": raw["source"],
        "fetched_at": raw.get("fetched_at"),
        "agent_id": meta["agent_id"],
        "checkpoint_id": meta["checkpoint_id"],
        "workflow_name": meta["workflow_name"],
        "root_span_id": meta["root_span_id"],
        "span_count": len(spans),
        "duration_ms": round((end_ns - start_ns) / 1e6, 2),
        "started_at": datetime.fromtimestamp(start_ns / 1e9, tz=UTC).isoformat(),
        "tree": _build_tree(spans, start_ns),
        "llm_spans": [
            {
                "span_id": sp["span_id"],
                "name": sp["name"],
                "model": sp["attributes"].get(_MODEL_ATTR),
                "start_offset_ms": round((sp["start_ns"] - start_ns) / 1e6, 2),
                "duration_ms": round((sp["end_ns"] - sp["start_ns"]) / 1e6, 2),
                "status": sp["status"],
            }
            for sp in spans
            if _is_llm(sp)
        ],
        "node_sequence": _node_sequence(spans, start_ns),
        "content": None,
        "events": None,
    }

    import urllib.error

    if args.with_content:
        if out["agent_id"] is None:
            print("--with-content: root span has no session.id; skipping")
        else:
            try:
                out["content"] = _fetch_content(args.gateway, out["agent_id"], out["trace_id"])
            except urllib.error.HTTPError as exc:
                print(
                    f"--with-content failed: HTTP {exc.code} "
                    f"(404 = agent gone; 401 = wrong/missing cluster secret)"
                )
    if args.with_events:
        from_iso = (datetime.fromtimestamp(start_ns / 1e9, tz=UTC) - timedelta(hours=1)).isoformat()
        try:
            resp = _fetch_events(args.gateway, out["trace_id"], from_iso)
            out["events"] = resp.get("items", [])
            out["events_total"] = resp.get("meta", {}).get("total")
        except urllib.error.HTTPError as exc:
            print(f"--with-events failed: HTTP {exc.code}")

    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    _print_summary(out)
    print(f"wrote -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
