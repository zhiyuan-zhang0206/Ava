"""Forbid reading per-agent config through a process-global in turn-scoped code.

Run: `.venv/bin/python scripts/lint/turn_scoped_config.py [path ...]` (defaults
to the turn-scoped packages; an explicit path that does not exist is an error
(stderr + exit 1) rather than a silent no-op). Also runs automatically via
pre-commit.

## Why

In the hosted runner model (future/infra/agent-runner-as-server.md, work item
b) many agents' turns share one process, so a `per_agent=True` field read
through the process-global `settings` singleton returns the CLUSTER default —
silently ignoring the agent's `config_overlay` / `birth_config`. The correct
read path for turn-scoped code is the agent's slices
(`base/host/env/agent_slices.py`, on `runtime.context.agent`):

    runtime.context.require_agent().brain.llm_model   # pin, else the live default

The exec child and the SDK run one agent per process, so their settings carry
that agent's overlay (boot applied it onto the singleton); the SDK reads a
per-agent setting through `ava._settings.agent_setting`.

## Rules

**Framework fields.** Scan the turn-scoped packages (code that runs inside an agent's turn):
`agent/`, `ava/`, `ava_builtins/`, `base/lm/`, plus the turn-adjacent
`base` modules listed in _EXTRA_FILES. Any `settings.<domain>.<field>`
attribute read where `<field>` is a `per_agent=True` field in the config
registry is an error — the site must read the agent's slices (or `agent_setting`).

The per-agent field set is read from the live config registry
(`base.config.per_agent_field_names`), so declaring a new per-agent field
auto-extends the ban with no manual list to maintain.

**Plugin config.** Same problem one layer over: `_PLUGIN_CONFIGS`
(`base/packages/plugins/config_registration.py`) is a process-global `plugin -> instance`
map that boot rebuilds from the agent's overlay, so subscripting it in turn
code returns whichever agent booted the process. Reads go through
`get_plugin_config(plugin, slices)` (host side) or
`process_plugin_config` (this process's own instance; `ava._settings.plugins`). Membership tests
(`name in _PLUGIN_CONFIGS`) are untouched — they ask whether a plugin is
registered, which is not per-agent.

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

from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

_SCAN_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "base/lm",
)

# Turn-adjacent `base` modules that execute inside agent turns.
_EXTRA_FILES = ("base/packages/plugins/activation.py",)

_ALLOWED_FILES = frozenset(
    {
        # The slices resolve a field's live default off the singleton by design.
        "base/host/env/agent_slices.py",
    }
)

# The two files that ARE the plugin-config mechanism: the registry owns the
# process-global map, the view layers an agent's overrides over it.
_PLUGIN_MECHANISM_FILES = frozenset(
    {
        "base/packages/plugins/config_registration.py",
        "base/packages/plugins/config_view.py",
    }
)

_SETTINGS_ATTR = re.compile(r"\bsettings\.([a-z_]+)\.([a-z_]+)")
# Subscript only — `name in _PLUGIN_CONFIGS` is a registration probe, not a read.
_PLUGIN_CONFIGS_READ = re.compile(r"\b_PLUGIN_CONFIGS\[")


def _iter_files(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths if p.endswith(".py")]
    files: list[Path] = []
    for d in _SCAN_DIRS:
        files.extend((_REPO_ROOT / d).rglob("*.py"))
    files.extend(_REPO_ROOT / f for f in _EXTRA_FILES)
    return files


# The config registry decides which fields are per-agent for every file.
_RULE_INPUTS = ("base/config/", "base/host/env/")


def main(argv: list[str]) -> int:
    from base.config import per_agent_field_names

    argv, only = lint_common.split_only(argv)
    missing = [a for a in argv if not Path(a).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    scope = lint_common.changed_scope(only, _REPO_ROOT, inputs=_RULE_INPUTS)
    per_agent = set(per_agent_field_names())
    errors: list[str] = []
    plugin_errors: list[str] = []
    for path in lint_common.restrict(_iter_files(argv), scope, _REPO_ROOT):
        resolved = path.resolve()
        try:
            rel = resolved.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = resolved.as_posix()
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
            if rel not in _PLUGIN_MECHANISM_FILES and _PLUGIN_CONFIGS_READ.search(line):
                plugin_errors.append(f"{rel}:{lineno}: {line.strip()}")
    if plugin_errors:
        print(
            "plugin config read straight out of the process-global "
            "_PLUGIN_CONFIGS in turn-scoped code — use "
            "`get_plugin_config(<plugin>, slices)` (or `process_plugin_config` in the "
            "exec child); in hosted mode the map holds whichever agent booted the "
            "process:\n",
            file=sys.stderr,
        )
        for e in plugin_errors:
            print(f"  {e}", file=sys.stderr)
    if errors:
        print(
            "per-agent config read through the bare settings singleton in "
            "turn-scoped code — read it from the agent's slices "
            "(base/host/env/agent_slices.py) or `ava._settings.agent_setting`; in "
            "hosted mode the singleton holds the CLUSTER default, not this agent's "
            "overlay:\n",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  {e}", file=sys.stderr)
    return 1 if (errors or plugin_errors) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
