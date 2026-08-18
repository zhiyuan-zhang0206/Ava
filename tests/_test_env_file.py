"""Test-home .env file sync helper (plain module — a function defined in
conftest.py is collected by pytest as a fixture, so this lives outside it)."""

from __future__ import annotations

from pathlib import Path


def rewrite_line(env_path: Path, key: str, value: str) -> None:
    """Replace (or append) one KEY=value line in a .env file, in place."""
    lines = env_path.read_text().splitlines()
    kept = [ln for ln in lines if not ln.startswith(f"{key}=")]
    kept.append(f"{key}={value}")
    env_path.write_text("\n".join(kept) + "\n")
