#!/usr/bin/env python3
"""Thin CLI entry point for the weekly/batch trace-dataset collector.

The collection logic lives in
`ava_builtins.skill_support.self_evolution.collect` (imported both by this
CLI and by sibling reference scripts — `daily_scan.py`, `mirror_backfill.py`,
`evaluate.py`, `mine.py`); see that module for the full docs.

Usage:
    .venv/bin/python skills/ava-self-evolution/reference/collect.py --days 7
"""

from __future__ import annotations

from ava_builtins.skill_support.self_evolution.collect import main

if __name__ == "__main__":
    main()
