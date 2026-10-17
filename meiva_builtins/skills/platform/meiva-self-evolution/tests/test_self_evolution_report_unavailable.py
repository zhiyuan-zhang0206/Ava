from __future__ import annotations

from typing import Any, cast

from meiva_builtins.skills.tests.skill_script import load_skill_script

render = cast(
    Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "render_report.py")
)


def test_unavailable_skill_renders_na() -> None:
    html = render._render_score_skills(
        {"score_skills": [{"skill": "demo", "dimensions": [], "n": 0, "unavailable": True}]}
    )
    assert "n/a" in html
    assert "0.0" not in html


def test_no_baseline_card_renders_na() -> None:
    html = render._render_score_skills(
        {"score_skills": [{"skill": "demo", "dimensions": [], "n": 12, "unavailable": False}]}
    )
    assert "n/a — no earlier stamp to compare" in html


def test_delta_average_ignores_unavailable_skills() -> None:
    data = render.build_report_data(
        week="2026-10-17",
        changes=[],
        score_skills=[
            {"skill": "scored", "dimensions": [{"label": "overall", "delta": 0.1}]},
            {"skill": "empty", "dimensions": [], "n": 0, "unavailable": True},
        ],
        failure_clusters=[],
    )
    assert data["overall_delta"] == "+0.100"
