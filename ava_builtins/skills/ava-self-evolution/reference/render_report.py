#!/usr/bin/env python3
"""Thin CLI entry point for rendering a self_evolution report JSON to HTML.

The rendering logic (and the `report_template.html` it renders against) lives
in `ava_builtins.skill_support.self_evolution.render_report` (also imported by
`aggregate.py` for `build_report_data`); see that module for the full docs.

Usage:
    .venv/bin/python skills/ava-self-evolution/reference/render_report.py <report.json> [--output report.html]
"""

from __future__ import annotations

from ava_builtins.skill_support.self_evolution.render_report import main

if __name__ == "__main__":
    main()
