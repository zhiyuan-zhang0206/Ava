#!/usr/bin/env python3
"""Thin CLI entry point for the local-mirror backfill (Loki dense-window fallback).

The backfill logic lives in
`ava_builtins.skill_support.self_evolution.mirror_backfill` (also imported by
`daily_scan.py` as the automatic no-observability fallback); see that module
for the full docs.

Usage:
    .venv/bin/python skills/ava-self-evolution/reference/mirror_backfill.py <days> [week]
"""

from __future__ import annotations

from ava_builtins.skill_support.self_evolution.mirror_backfill import main

if __name__ == "__main__":
    main()
