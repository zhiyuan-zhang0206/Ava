"""No production entry point imports the retained-image release path.

`pyproject.toml` names the path in the import-linter contract below: image
preparation and verification, the frozen image-exec handoff, the fleet
coordinator, the release transition and its PITR operation, write-generation
rotation and the per-unit enrollment channel. Import-linter proves statically
that no production module names it (deferred imports included); this test proves
it dynamically: a fresh interpreter imports every production entry point, and
`sys.modules` must hold none of the path's modules afterwards.

The entry points are the ones the running system launches: every `-m` module of
the service roster (`ops.roster.build_services`), every module an `ava` verb's
handler imports (`cli/parsers`), and the few children and supervisors that no
roster row or parser names.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_CONTRACT = "Production must not import the release/image path"

# Launched by a parent process, a supervisor or an import alias rather than by a
# roster row or a parser handler. `python -m gateway` runs `gateway/__main__.py`,
# which only calls `gateway.app.main`.
_UNLISTED_ENTRIES = (
    "cli.main",
    "gateway.app",
    "gateway._server",
    "services.ava_root.daemon",
    "services.ava_root_glue.glue",
    "services.ava_root_glue.manifests",
    "services.healthchecks.mcp_daemon",
    "services.hierarchy_worker.job",
    "agent.exec_child",
    "agent.process_boot",
    "base._reparent",
    "base.sessions.pty.cli",
    "base.sessions.pty.host",
)
_ROSTER_ALIASES = {"gateway": "gateway.app"}

_PROBE = """
import importlib
import json
import sys

spec = json.load(sys.stdin)
sys.argv = ["release-path-boundary-probe"]
release = tuple(spec["release"])


def loaded_release_modules():
    return {
        name
        for name in sys.modules
        if any(name == root or name.startswith(root + ".") for root in release)
    }


seen = set()
first_leak = {}
for entry in spec["entries"]:
    importlib.import_module(entry)
    fresh = loaded_release_modules() - seen
    if fresh:
        first_leak[entry] = sorted(fresh)
        seen |= fresh
print(json.dumps(first_leak))
"""


def _release_modules() -> tuple[str, ...]:
    contracts = tomllib.loads((_REPO / "pyproject.toml").read_text())["tool"]["importlinter"][
        "contracts"
    ]
    (contract,) = (item for item in contracts if item["name"] == _CONTRACT)
    return tuple(contract["forbidden_modules"])


def _roster_entries() -> set[str]:
    from ops.roster import build_services

    modules: set[str] = set()
    for spec in build_services():
        match = re.search(r"\s-m (\S+)", " " + spec.cmd)
        if match is not None:
            modules.add(_ROSTER_ALIASES.get(match[1], match[1]))
    return modules


def _parser_handler_entries() -> set[str]:
    """Every module a verb handler imports lazily, named in `cli/parsers/*.py`."""
    modules: set[str] = set()
    for path in (_REPO / "cli" / "parsers").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("cli."):
                modules.add(node.module or "")
    return modules


def _entries() -> list[str]:
    return sorted(_roster_entries() | _parser_handler_entries() | set(_UNLISTED_ENTRIES))


def test_the_contract_names_only_existing_modules() -> None:
    """A renamed or deleted member must leave the list, or the proof below goes vacuous."""
    missing = [name for name in _release_modules() if importlib.util.find_spec(name) is None]
    assert missing == []


def test_every_daemon_module_is_an_entry_point() -> None:
    """A new daemon joins the roster or the unlisted set; it cannot skip the boundary."""
    entries = set(_entries())
    daemons = {
        ".".join(path.relative_to(_REPO).with_suffix("").parts)
        for root in ("services", "ava", "ava_builtins")
        for pattern in ("daemon.py", "*_daemon.py")
        for path in (_REPO / root).rglob(pattern)
    }
    assert sorted(daemons - entries) == []


def test_production_entry_points_never_import_the_release_path() -> None:
    entries = _entries()
    assert len(entries) > 40
    result = subprocess.run(  # noqa: S603 — fixed interpreter and program; entries are repo module names.
        [sys.executable, "-B", "-c", _PROBE],
        input=json.dumps({"release": _release_modules(), "entries": entries}),
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    assert json.loads(result.stdout.splitlines()[-1]) == {}
