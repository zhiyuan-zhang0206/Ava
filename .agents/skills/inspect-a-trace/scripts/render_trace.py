#!/usr/bin/env python3
"""Render a read trace (trace_read.json) as a self-contained HTML report.

Output: `<out>/index.html` — inline CSS and JS, no CDN, no build step, so the
served page opens offline. Serve it with
`ava.ui.serve(<out>, name="trace-<agent>-<short-id>")`.

Sections: header, waterfall timeline (click a bar for its attributes), node
sequence, LLM span table, event stream, and the turn content when present.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path

# Grafana's browser entry is the authenticated gateway subpath, never the
# loopback :3003 upstream. AVA_GRAFANA_URL remains an explicit report override.
_GATEWAY = os.environ.get("AVA_GATEWAY_URL", "http://localhost:8000").rstrip("/")
_GRAFANA = os.environ.get("AVA_GRAFANA_URL", f"{_GATEWAY}/grafana")


def _esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def _fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.0f}ms"


def _waterfall(data: dict) -> str:
    dur = data["duration_ms"] or 1
    rows: list[str] = []

    def walk(nodes: list[dict]) -> None:
        for n in nodes:
            left = n["start_offset_ms"] / dur * 100
            width = max(n["duration_ms"] / dur * 100, 0.08)
            cls = "bar llm" if n["name"].endswith(".chat") else "bar"
            if n["status"] == "ERROR":
                cls += " err"
            label = _esc(n["name"])
            rows.append(
                f'<div class="row" style="--depth:{n["depth"]}" '
                f"data-attrs='{_esc(json.dumps(n))}'>"
                f'<div class="rname" title="{label}">{label}</div>'
                f'<div class="rtrack"><div class="{cls}" '
                f'style="left:{left:.3f}%;width:{width:.3f}%"></div></div>'
                f'<div class="rdur">{_fmt_ms(n["duration_ms"])}</div></div>'
            )
            walk(n["children"])

    walk(data["tree"])
    return "\n".join(rows)


def _llm_table(data: dict) -> str:
    rows = []
    for llm in data["llm_spans"]:
        rows.append(
            "<tr>"
            f"<td>{_esc(llm['name'])}</td>"
            f"<td>{_esc(llm['model'])}</td>"
            f"<td class='num'>+{llm['start_offset_ms']:.0f}ms</td>"
            f"<td class='num'>{_fmt_ms(llm['duration_ms'])}</td>"
            f"<td>{_esc(llm['status'])}</td>"
            "</tr>"
        )
    return "\n".join(rows) or "<tr><td colspan=5>none</td></tr>"


def _node_table(data: dict) -> str:
    rows = []
    for n in data["node_sequence"]:
        rows.append(
            "<tr>"
            f"<td class='num'>{n['step']}</td>"
            f"<td>{_esc(n['node'])}</td>"
            f"<td class='num'>+{n['start_offset_ms']:.0f}ms</td>"
            f"<td class='num'>{_fmt_ms(n['duration_ms'])}</td>"
            "</tr>"
        )
    return "\n".join(rows) or "<tr><td colspan=4>none</td></tr>"


def _events_table(data: dict) -> str:
    events = data.get("events") or []
    rows = []
    for ev in events[:200]:
        attrs = _esc(json.dumps(ev.get("attributes"))[:220])
        rows.append(
            "<tr>"
            f"<td>{_esc(ev.get('ts'))}</td>"
            f"<td>{_esc(ev.get('category'))}</td>"
            f"<td>{_esc(ev.get('event_name'))}</td>"
            f"<td>{_esc(ev.get('level'))}</td>"
            f"<td title='{attrs}'>{_esc(ev.get('machine'))}</td>"
            "</tr>"
        )
    note = f"showing {len(rows)} of {data.get('events_total', len(rows))}"
    return f"<p class='note'>{note}</p>\n<table>{'\n'.join(rows)}</table>"


def _content_section(data: dict) -> str:
    content = data.get("content")
    if content is None:
        return "<p class='note'>content not fetched — re-run read_trace.py with --with-content</p>"
    if content.get("pruned"):
        return "<p class='note'>checkpoint pruned — turn content unavailable (expected for old turns)</p>"
    msgs = content.get("messages", [])
    blocks = []
    for m in msgs[:60]:
        role = _esc(m.get("role") or m.get("type"))
        text = _esc(str(m.get("content"))[:800])
        blocks.append(f'<div class="msg"><span class="mrole">{role}</span>{text}</div>')
    head = f"<p class='note'>{len(msgs)} messages (showing 60)</p>"
    return head + "\n".join(blocks)


def _grafana_link(data: dict) -> str:
    import urllib.parse

    start = data.get("started_at", "")
    pane = {
        "explore": {
            "datasource": "tempo",
            "queries": [{"refId": "A", "queryType": "traceId", "query": data["trace_id"]}],
            "range": {"from": f"{start[:-6]}Z" if start else "now-1d", "to": "now"},
        }
    }
    q = urllib.parse.quote(json.dumps(pane, separators=(",", ":")))
    return f"{_GRAFANA}/explore?schemaVersion=1&panes={q}&orgId=1"


def render(data: dict) -> str:
    d = data
    wf = _waterfall(d)
    link = _grafana_link(d)
    title = f"Trace {d['trace_id'][:12]} — {_esc(d.get('agent_id'))} {_esc(d.get('workflow_name') or '')}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#fafafa; --ink:#1a1a1a; --muted:#6b7280; --line:#e5e7eb;
        --bar:#6366f1; --llm:#f59e0b; --err:#dc2626; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
header {{ padding:20px 24px; border-bottom:1px solid var(--line); background:#fff; }}
h1 {{ font-size:18px; margin:0 0 6px; }}
.meta {{ color:var(--muted); font-size:13px; }}
.meta b {{ color:var(--ink); }}
main {{ max-width:1200px; margin:0 auto; padding:16px 24px 60px; }}
section {{ background:#fff; border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; margin:16px 0; }}
h2 {{ font-size:14px; margin:0 0 10px; }}
a {{ color:#4f46e5; }}
.row {{ display:flex; align-items:center; gap:10px; padding:1px 0;
  cursor:pointer; border-radius:4px; }}
.row:hover {{ background:#f3f4f6; }}
.rname {{ width:34%; min-width:220px; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; padding-left:calc(var(--depth) * 14px); font-size:12.5px; }}
.rtrack {{ flex:1; position:relative; height:14px; background:#f3f4f6;
  border-radius:3px; }}
.bar {{ position:absolute; top:2px; height:10px; border-radius:2px;
  background:var(--bar); }}
.bar.llm {{ background:var(--llm); }}
.bar.err {{ background:var(--err); }}
.rdur {{ width:70px; text-align:right; color:var(--muted); font-size:12px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
td,th {{ text-align:left; padding:4px 10px; border-bottom:1px solid var(--line);
  vertical-align:top; }}
th {{ color:var(--muted); font-weight:600; }}
td.num,th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.note {{ color:var(--muted); font-size:13px; }}
#detail {{ display:none; margin-top:10px; padding:10px; background:#f9fafb;
  border:1px solid var(--line); border-radius:6px; white-space:pre-wrap;
  font:12px/1.45 ui-monospace,Menlo,monospace; }}
.msg {{ border-left:3px solid var(--line); margin:8px 0; padding:4px 10px;
  font-size:13px; white-space:pre-wrap; }}
.mrole {{ display:block; color:var(--muted); font-size:11px;
  text-transform:uppercase; letter-spacing:.4px; }}
</style></head><body>
<header>
  <h1>{title}</h1>
  <div class="meta">
    id <b>{_esc(d["trace_id"])}</b> · {_esc(d.get("span_count"))} spans ·
    {_fmt_ms(d["duration_ms"])} · source {_esc(d.get("source"))} ·
    checkpoint {_esc(d.get("checkpoint_id"))} · started {_esc(d.get("started_at"))}
    · fetched {_esc(d.get("fetched_at"))}<br>
    <a href="{_esc(link)}">open in Grafana Explore</a> (interactive Tempo browser)
  </div>
</header>
<main>
<section><h2>Waterfall</h2><div id="wf">{wf}</div>
<div id="detail"></div></section>
<section><h2>LLM calls ({len(d["llm_spans"])})</h2>
<table><tr><th>span</th><th>model</th><th class="num">offset</th>
<th class="num">duration</th><th>status</th></tr>{_llm_table(d)}</table></section>
<section><h2>Node sequence ({len(d["node_sequence"])})</h2>
<table><tr><th class="num">step</th><th>node</th><th class="num">offset</th>
<th class="num">duration</th></tr>{_node_table(d)}</table></section>
<section><h2>Events</h2>{_events_table(d)}</section>
<section><h2>Turn content</h2>{_content_section(d)}</section>
</main>
<script>
const wf = document.getElementById('wf');
const detail = document.getElementById('detail');
wf.addEventListener('click', e => {{
  const row = e.target.closest('.row');
  if (!row) return;
  const attrs = JSON.parse(row.dataset.attrs);
  detail.style.display = 'block';
  detail.textContent = JSON.stringify(attrs, null, 2);
}});
</script>
</body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[1])
    ap.add_argument("read", help="trace_read.json from read_trace.py")
    ap.add_argument("--out", default="report", help="output directory for index.html")
    args = ap.parse_args()

    try:
        data = json.loads(Path(args.read).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"no such file: {args.read}")
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    page = out_dir / "index.html"
    page.write_text(render(data), encoding="utf-8")
    print(f"wrote {page}")
    print("serve it with: ava.ui.serve(<out>, name='trace-<agent>-<short-id>')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
