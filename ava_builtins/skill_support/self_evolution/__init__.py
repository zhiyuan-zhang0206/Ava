"""Shared logic for the ava-self-evolution skill's reference scripts.

`ava_builtins/skills/ava-self-evolution/reference/` holds the skill's thin
CLI entry points (`collect.py`, `mirror_backfill.py`, `render_report.py`,
plus the standalone `daily_scan.py`, `evaluate.py`, `mine.py`, `aggregate.py`);
this package holds the logic and helpers those scripts import instead of
loading each other by file path.
"""
