#!/usr/bin/env python3
"""Render a self_evolution report JSON into a self-contained HTML page.

Reads a JSON report file and renders it against `report_template.html` into a
standalone HTML page. The template uses `<!-- SECTION_NAME -->...<!-- END_SECTION_NAME -->`
markers; each section is replaced with the corresponding rendered content.

Usage:
    .venv/bin/python skills/ava-self-evolution/reference/render_report.py <report.json> [--output report.html]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Template ────────────────────────────────────────────────────────

_TEMPLATE_CACHE_CACHE: str | None = None


def _load_template() -> str:
    if _TEMPLATE_CACHE_CACHE is None:
        tpl_path = Path(__file__).resolve().parent / "report_template.html"
        # Reassign via module-level name (no `global` needed — we are not rebinding
        # a local, just mutating the module's own attribute).
        globals()["_TEMPLATE_CACHE_CACHE"] = tpl_path.read_text(encoding="utf-8")
    return _TEMPLATE_CACHE_CACHE


# ── Section renderers ───────────────────────────────────────────────


def _render_score_skills(data: dict[str, Any]) -> str:
    skills = data.get("score_skills")
    if not skills:
        return """<div class="empty">No score comparison data for this round.</div>"""

    cards = []
    for s in skills:
        dims_html = []
        for d in s.get("dimensions", []):
            dims_html.append(
                f"""<div class="score-row">
      <span class="score-dim">{d["label"]}</span>
      <span class="score-before">{d["before"]}</span>
      <span class="score-arrow">→</span>
      <span class="score-after">{d["after"]}</span>
      <div class="score-bar-wrap">
        <div class="score-bar-before" style="width:{min(d["before_pct"], 95)}%"></div>
        <div class="score-bar-after" style="width:{abs(d["delta_pct"])}%"></div>
      </div>
      <span class="score-delta {d["delta_class"]}">{d["delta_str"]}</span>
    </div>"""
            )
        cards.append(
            f"""<div class="score-card">
    <div class="score-skill-name">{s["skill"]}</div>
    {"".join(dims_html)}
  </div>"""
        )

    return f"""<div class="score-grid">
  {"".join(cards)}
</div>"""


def _render_changes_table(data: dict[str, Any]) -> str:
    changes = data.get("changes")
    if not changes:
        return """<div class="empty">No changes recorded this round.</div>"""

    rows = []
    for c in changes:
        pr_cell = (
            f'<a href="{c["pr_url"]}" style="color:#58a6ff">#{c["pr"]}</a>'
            if c.get("pr") and c.get("pr_url")
            else (f"#{c['pr']}" if c.get("pr") else "&mdash;")
        )
        rows.append(
            f"""<tr>
      <td style="font-weight:500;color:#f0f6fc">{c["skill"]}</td>
      <td>{c["summary"]}</td>
      <td style="font-family:monospace;font-size:0.8rem">{pr_cell}</td>
    </tr>"""
        )

    return f"""<table>
  <thead><tr><th>Skill</th><th>Summary</th><th>PR</th></tr></thead>
  <tbody>
  {"".join(rows)}
  </tbody>
</table>"""


def _render_failure_clusters(data: dict[str, Any]) -> str:
    clusters = data.get("failure_clusters")
    if not clusters:
        return """<div class="empty">No failures this week — all runs completed cleanly. 🎉</div>"""

    parts = []
    for cl in clusters:
        # Badges
        badges = []
        if cl.get("failed", 0) > 0:
            badges.append(f'<span class="cluster-badge badge-failed">{cl["failed"]} failed</span>')
        if cl.get("fumbled", 0) > 0:
            badges.append(
                f'<span class="cluster-badge badge-fumbled">{cl["fumbled"]} fumbled</span>'
            )

        # Signals
        signals_html = ""
        if cl.get("top_signals"):
            tags = "".join(f'<span class="signal-tag">{s}</span>' for s in cl["top_signals"])
            signals_html = f'<div class="cluster-signals">{tags}</div>'

        # Runs (show up to 8, note overflow)
        runs_html = ""
        if cl.get("runs"):
            shown = cl["runs"][:8]
            run_items = []
            for r in shown:
                label_cls = "label-failed" if r.get("label") == "failed" else "label-fumbled"
                run_items.append(
                    f"""<div class="run-item">
      #{r["agent_id"]}
      <span class="run-label {label_cls}">[{r["label"]}]</span>
      &mdash; {r.get("task_snippet", "")}
    </div>"""
                )
            overflow = ""
            if len(cl["runs"]) > 8:
                overflow = (
                    f'<div class="run-item" style="color:#484f58">'
                    f"... and {len(cl['runs']) - 8} more</div>"
                )
            runs_html = f"""<div class="run-list">
  {"".join(run_items)}
  {overflow}
  </div>"""

        parts.append(
            f"""<div class="cluster">
  <div class="cluster-header">
    <span class="cluster-skill">{cl["skill"]}</span>
    <span class="cluster-count">
      {cl["count"]} bad runs
      {"".join(badges)}
    </span>
  </div>
  {signals_html}
  {runs_html}
</div>"""
        )

    return "\n".join(parts)


def _render_eval_results(data: dict[str, Any]) -> str:
    er = data.get("eval_results")
    if not er:
        return """<div class="empty">No eval data for this round.</div>"""

    def _verdict_color(v: str) -> str:
        if v in ("better", "improved"):
            return "#3fb950"
        if v in ("worse", "regressed"):
            return "#f85149"
        return "#8b949e"

    summary = f"""<div class="eval-summary">
  <div class="eval-stat">
    <div class="eval-stat-value">{er.get("replayed", 0)}</div>
    <div class="eval-stat-label">tasks replayed</div>
  </div>
  <div class="eval-stat">
    <div class="eval-stat-value" style="color:#f85149">{er.get("old_ok", 0)}</div>
    <div class="eval-stat-label">before (ok)</div>
  </div>
  <div class="eval-stat">
    <div class="eval-stat-value" style="color:#3fb950">{er.get("new_ok", 0)}</div>
    <div class="eval-stat-label">after (ok)</div>
  </div>
  <div class="eval-stat">
    <div class="eval-stat-value" style="color:#3fb950">{er.get("improved", 0)}</div>
    <div class="eval-stat-label">improved</div>
  </div>
  <div class="eval-stat">
    <div class="eval-stat-value" style="color:#f85149">{er.get("regressed", 0)}</div>
    <div class="eval-stat-label">regressed</div>
  </div>
</div>"""

    per_skill = er.get("per_skill")
    per_skill_html = ""
    if per_skill:
        rows = []
        for es in per_skill:
            v = es.get("verdict", "")
            rows.append(
                f"""<tr>
      <td style="font-weight:500;color:#f0f6fc">{es["skill"]}</td>
      <td>{es.get("replayed", 0)}</td>
      <td style="color:#3fb950">{es.get("improved", 0)}</td>
      <td style="color:#f85149">{es.get("regressed", 0)}</td>
      <td><span style="color:{_verdict_color(v)}">{v}</span></td>
    </tr>"""
            )
        per_skill_html = f"""<table>
  <thead><tr><th>Skill</th><th>Replayed</th><th>Improved</th><th>Regressed</th><th>Verdict</th></tr></thead>
  <tbody>
  {"".join(rows)}
  </tbody>
</table>"""

    return summary + per_skill_html


# ── Render ──────────────────────────────────────────────────────────


def render_report(data: dict[str, Any]) -> str:
    """Render the full HTML report from assembled data."""
    template = _load_template()

    # Substitute simple {{ var }} placeholders
    def _sub(m: re.Match) -> str:
        key = m.group(1).strip()
        val = data.get(key)
        if val is None:
            return ""
        if isinstance(val, bool):
            return str(val).lower()
        return str(val)

    html = re.sub(r"\{\{\s*(.+?)\s*\}\}", _sub, template)

    # Replace sections
    sections = {
        "SCORE_SKILLS": _render_score_skills(data),
        "CHANGES_TABLE": _render_changes_table(data),
        "FAILURE_CLUSTERS": _render_failure_clusters(data),
        "EVAL_RESULTS": _render_eval_results(data),
    }

    for name, content in sections.items():
        html = re.sub(
            rf"<!-- {name} -->.*?<!-- END_{name} -->",
            f"<!-- {name} -->\n{content}\n<!-- END_{name} -->",
            html,
            flags=re.DOTALL,
        )

    return html


# ── Data builder (convenience) ──────────────────────────────────────


def build_report_data(
    *,
    week: str,
    changes: list[dict[str, str]],
    score_skills: list[dict[str, Any]],
    failure_clusters: list[dict[str, Any]],
    eval_results: dict[str, Any] | None = None,
    total_runs: int = 0,
) -> dict[str, Any]:
    """Assemble the data context consumed by `render_report`.

    See the JSON schema documented in the template's companion README or the
    sample at `skills/ava-self-evolution/reference/sample_report.json`.
    """
    overall_delta = 0.0
    for s in score_skills:
        for d in s.get("dimensions", []):
            if d.get("label") == "overall":
                overall_delta += d.get("delta", 0)
    if score_skills:
        overall_delta = round(overall_delta / len(score_skills), 3)

    tasks_replayed = eval_results.get("replayed", 0) if eval_results else 0
    improved = eval_results.get("improved", 0) if eval_results else 0
    regressed = eval_results.get("regressed", 0) if eval_results else 0

    return {
        "week": week,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "skill_count": len(score_skills),
        "total_runs": total_runs,
        "changes_count": len(changes),
        "overall_delta": f"{overall_delta:+.3f}",
        "tasks_replayed": tasks_replayed,
        "improved": improved,
        "regressed": regressed,
        "cluster_count": len(failure_clusters),
        "cluster_skills": len({c["skill"] for c in failure_clusters}),
        "changes": changes,
        "score_skills": score_skills,
        "failure_clusters": failure_clusters,
        "eval_results": eval_results,
    }


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description="Render a self_evolution report JSON to HTML.")
    p.add_argument("report_json", help="path to the aggregated report JSON")
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help="output HTML path (default: <report>.html)",
    )
    args = p.parse_args()

    in_path = Path(args.report_json)
    data = json.loads(in_path.read_text(encoding="utf-8"))
    html = render_report(data)

    out_path = Path(args.output) if args.output else in_path.with_suffix(".html")
    out_path.write_text(html, encoding="utf-8")
    print(f"Rendered {out_path} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
