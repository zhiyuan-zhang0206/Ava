from __future__ import annotations

import re
from pathlib import Path

from meizel.roles import packages

_REPO = Path(__file__).resolve().parents[3]

_SCAN_DIRS = packages("application")

_REAPER_STAMP = re.compile(r"termination_source\s*=\s*'reaper'")
_MARKER_CLEAR = re.compile(r"last_turn_fatal_at\s*=\s*NULL")
_MARKER_STAMP = re.compile(r"COALESCE\(\s*last_turn_fatal_at\s*,\s*clock_timestamp\(\)\s*\)")
_UPDATE_AGENTS_META = "UPDATE agents_meta"


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in _SCAN_DIRS:
        files.extend(
            path
            for path in sorted((_REPO / directory).rglob("*.py"))
            if "tests" not in path.relative_to(_REPO).parts
        )
    return files


def test_every_reaper_write_preserves_the_crash_marker() -> None:
    checked: list[str] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for match in _REAPER_STAMP.finditer(text):
            if _UPDATE_AGENTS_META not in text[max(0, match.start() - 600) : match.start()]:
                continue
            rel = path.relative_to(_REPO).as_posix()
            window = text[max(0, match.start() - 600) : match.end() + 900]
            assert "last_turn_fatal_at IS NOT NULL" in window, (
                f"{rel}: a `termination_source = 'reaper'` write no longer "
                "requires the crash marker — SYSTEM_REAPED_CRASH_ROW would "
                "match more rows than intended"
            )
            assert not _MARKER_CLEAR.search(window), (
                f"{rel}: a `termination_source = 'reaper'` write clears "
                "last_turn_fatal_at in the same statement — reaper rows would "
                "stop matching SYSTEM_REAPED_CRASH_ROW"
            )
            checked.append(rel)
    assert checked, "no reaper write sites found — did the literal change?"
    assert "agent/ownership/corpse_reap.py" in checked


def test_marker_clear_sites_are_enumerated() -> None:
    clearers = {
        path.relative_to(_REPO).as_posix()
        for path in _python_files()
        if _MARKER_CLEAR.search(path.read_text(encoding="utf-8"))
    }
    assert clearers == {
        "agent/graph/llm/node.py",
        "ops/agents/wake.py",
    }


def test_marker_stamp_sites_are_enumerated() -> None:
    stampers = {
        path.relative_to(_REPO).as_posix()
        for path in _python_files()
        if _MARKER_STAMP.search(path.read_text(encoding="utf-8"))
    }
    assert stampers == {"agent/ownership/hosted.py"}


def test_relaxed_guard_consumers_embed_the_shared_predicates() -> None:
    expectations = {
        "ops/agents/wake.py": ["RECOVERY_BREAKER_CLEAR"],
    }
    for rel, names in expectations.items():
        text = (_REPO / rel).read_text(encoding="utf-8")
        for name in names:
            assert name in text, f"{rel} lost its {name} reference"
