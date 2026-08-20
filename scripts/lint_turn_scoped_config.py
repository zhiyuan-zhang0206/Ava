"""Forbid reading per-agent config through the bare settings singleton in turn-scoped code.

Run: `.venv/bin/python scripts/lint_turn_scoped_config.py [path ...]` (defaults
to the turn-scoped packages). Also runs automatically via pre-commit.

## Why

In the hosted runner model (future/infra/agent-runner-as-server.md, work item
b) many agents' turns share one process, so a `per_agent=True` field read
through the process-global `settings` singleton returns the CLUSTER default —
silently ignoring the agent's `config_overlay` / `birth_config`. The correct
read path for turn-scoped code is the per-turn view:

    from shared.config.turn_view import turn_settings
    turn_settings.lm.llm_model        # pin-aware: overlay > birth > live default

In process mode the view is byte-for-byte the singleton (boot applied the
overlay onto it), so the conversion is always safe; in hosted mode it is the
only correct read.

## Rule

Scan the turn-scoped packages (code that runs inside an agent's turn):
`agent/`, `ava/`, `ava_builtins/`, `shared/lm/`, plus the turn-adjacent
shared modules listed in _EXTRA_FILES. Any `settings.<domain>.<field>`
attribute read where `<field>` is a `per_agent=True` field in the config
registry is an error — the site must read `turn_settings.<domain>.<field>`.

The per-agent field set is read from the live config registry
(`shared.config.per_agent_field_names`), so declaring a new per-agent field
auto-extends the ban with no manual list to maintain.

Comment lines are skipped (docstrings inside the scan are matched — a
docstring showing the wrong pattern teaches the wrong pattern). Gateway / ops
code is NOT scanned: those processes legitimately read per-agent fields as
cluster defaults (spawn-time birth_config resolution, config panels).

## Exemptions

_ALLOWED_FILES only — a file may be exempt when it IS the mechanism (the view
itself, the boot-time overlay apply that writes the singleton). No inline
escape hatch.

Error format `file:line: <line content>` + non-zero exit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCAN_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "shared/lm",
)

# Turn-adjacent shared modules that execute inside agent turns.
_EXTRA_FILES = ("shared/plugin_activation.py",)

_ALLOWED_FILES = frozenset(
    {
        # The view itself falls through to the singleton by design.
        "shared/config/turn_view.py",
    }
)

_SETTINGS_ATTR = re.compile(r"\bsettings\.([a-z_]+)\.([a-z_]+)")


def _iter_files(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths if p.endswith(".py")]
    files: list[Path] = []
    for d in _SCAN_DIRS:
        files.extend((_REPO_ROOT / d).rglob("*.py"))
    files.extend(_REPO_ROOT / f for f in _EXTRA_FILES)
    return files


def main(argv: list[str]) -> int:
    from shared.config import per_agent_field_names

    per_agent = set(per_agent_field_names())
    errors: list[str] = []
    for path in _iter_files(argv):
        rel = path.resolve().relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWED_FILES or "/tests/" in rel or rel.startswith("tests/"):
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            for m in _SETTINGS_ATTR.finditer(line):
                if m.group(2) in per_agent:
                    errors.append(f"{rel}:{lineno}: {line.strip()}")
    if errors:
        print(
            "per-agent config read through the bare settings singleton in "
            "turn-scoped code — use `turn_settings.<domain>.<field>` "
            "(shared/config/turn_view.py); in hosted mode the singleton holds "
            "the CLUSTER default, not this agent's overlay:\n",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
