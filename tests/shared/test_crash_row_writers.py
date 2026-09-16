"""Enumeration guard for the crash-row predicate's write side (task #3617).

`shared.lifecycle_acceptance.SYSTEM_REAPED_CRASH_ROW` reads exactly two row
fields — `termination_source = 'reaper'` and `last_turn_fatal_at IS NOT NULL`.
This test enumerates the production write sites for both fields and their
clears, so a NEW writer/clearer cannot silently change what the predicate
means without this list being updated (the review's write-side enumeration,
design #3610 section 6).

The checks are invariant-shaped rather than a pinned per-statement list:

- every `termination_source = 'reaper'` write must also require
  `last_turn_fatal_at IS NOT NULL` nearby and must never clear the marker in
  the same statement — i.e. every reaper row satisfies the predicate;
- the crash marker is cleared in exactly the enumerated files;
- the crash marker is stamped in exactly the enumerated files.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# Mirrors scripts/lint_termination_source.py's scope: production trees only.
_SCAN_DIRS = ("agent", "ava", "ava_builtins", "cli", "gateway", "ops", "services", "shared")

_REAPER_STAMP = re.compile(r"termination_source\s*=\s*'reaper'")
_MARKER_CLEAR = re.compile(r"last_turn_fatal_at\s*=\s*NULL")
_MARKER_STAMP = re.compile(r"COALESCE\(\s*last_turn_fatal_at\s*,\s*clock_timestamp\(\)\s*\)")
# A match only counts as a write site when an `UPDATE agents_meta` statement
# opens shortly before it (string-concat gaps included); the same literal also
# appears in predicate definitions and event documentation.
_UPDATE_AGENTS_META = "UPDATE agents_meta"


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in _SCAN_DIRS:
        files.extend(sorted((_REPO / directory).rglob("*.py")))
    return files


def test_every_reaper_write_preserves_the_crash_marker() -> None:
    """A reaper write that skips the marker or clears it makes the relaxed
    trigger match rows it must not match."""
    checked: list[str] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for match in _REAPER_STAMP.finditer(text):
            if _UPDATE_AGENTS_META not in text[max(0, match.start() - 600) : match.start()]:
                continue  # a predicate definition or a doc mention, not a write
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
    assert "agent/corpse_reap.py" in checked  # the corpse reaper itself


def test_marker_clear_sites_are_enumerated() -> None:
    clearers = {
        path.relative_to(_REPO).as_posix()
        for path in _python_files()
        if _MARKER_CLEAR.search(path.read_text(encoding="utf-8"))
    }
    assert clearers == {
        "agent/graph/_llm.py",  # a completed LLM turn — the single reset
        "ops/agent_wake.py",  # the resurrect transition (per-death)
    }


def test_marker_stamp_sites_are_enumerated() -> None:
    stampers = {
        path.relative_to(_REPO).as_posix()
        for path in _python_files()
        if _MARKER_STAMP.search(path.read_text(encoding="utf-8"))
    }
    assert stampers == {"agent/hosted_ownership.py"}


def test_relaxed_guard_consumers_embed_the_shared_predicates() -> None:
    """The two relaxed CAS sites and the automatic-recovery gates must use the
    shared constants; a re-inlined literal would drift from the predicate."""
    expectations = {
        "services/delivery_watchdog/daemon.py": [
            "SYSTEM_REAPED_CRASH_ROW",
            "RECOVERY_BREAKER_CLEAR",
        ],
        "ops/agent_wake.py": ["SYSTEM_REAPED_CRASH_ROW", "RECOVERY_BREAKER_CLEAR"],
        "services/delivery_watchdog/dispatch_guard.py": ["RECOVERY_BREAKER_CLEAR"],
    }
    for rel, names in expectations.items():
        text = (_REPO / rel).read_text(encoding="utf-8")
        for name in names:
            assert name in text, f"{rel} lost its {name} reference"
