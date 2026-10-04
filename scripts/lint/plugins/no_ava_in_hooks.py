"""Forbid `import ava` in the modules where graph hooks live.

Run: `.venv/bin/python scripts/lint/plugins/no_ava_in_hooks.py [path ...]` (defaults to the
first-party packages; an explicit path that does not exist is an error). Also run by
pre-commit.

## Why

A graph hook runs in the agent host, a process serving many agents' turns. It operates on
the graph `state` it was handed and returns an update dict for LangGraph's reducer. The
`ava` SDK is what agent code calls inside its exec child: its state slot (`ava.state`)
exists only there, and its other state (identity, the exec turn) is per-process. A hook that
imports `ava` reaches for SDK state that is not that agent's — or not there at all, which
is how the ava_code after_exec hook read `ava.state` in the host, got nothing, and silently
injected nothing. Keeping `ava` out of hook modules makes the mistake impossible to write.

## Rule

A module is a hook module when it is any of:

- under `agent/hooks/`;
- named `agent_runtime.py` (a plugin's host-side face: its hooks and contributions);
- a module that defines a class deriving from `Hook` (`class X(Hook)` / `class X(hooks.Hook)`).

No import of `ava` or `ava.<anything>` may appear anywhere in it, including inside a
function or under `TYPE_CHECKING`. State a hook needs comes from its `state` argument
(`PluginStateHandle.view` / `.delta` for plugin state); an SDK-derived value it needs is
computed by a helper in a module that is not a hook module. There is no exemption and no
allowlist. Tests are not scanned.

Known gap: an `importlib.import_module("ava...")` string or a module a hook module imports
that itself imports `ava` is not seen — the rule is about the hook module's own imports.
Error format `file:line: <detail>`, non-zero exit.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

_SCAN_DIRS = (
    "agent",
    "ava",
    "ava_builtins",
    "base",
    "cli",
    "demos",
    "gateway",
    "ops",
    "schedules",
    "services",
)
_HOOKS_DIR = "agent/hooks/"
_FACE_NAME = "agent_runtime.py"


def _imports_ava(node: ast.AST) -> str | None:
    """The offending import statement's text when `node` imports `ava` (absolute), else None."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "ava" or alias.name.startswith("ava."):
                return f"import {alias.name}"
    elif (
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module is not None
        and (node.module == "ava" or node.module.startswith("ava."))
    ):
        return f"from {node.module} import ..."
    return None


def _defines_hook(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if (isinstance(base, ast.Name) and base.id == "Hook") or (
                isinstance(base, ast.Attribute) and base.attr == "Hook"
            ):
                return True
    return False


def _scan_file(path: Path, rel: str) -> list[tuple[int, str]]:
    """[(lineno, message), ...] for `ava` imports in a hook module; empty for any other file."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []  # unreadable or broken files fail elsewhere; not this lint's job
    if not (rel.startswith(_HOOKS_DIR) or path.name == _FACE_NAME or _defines_hook(tree)):
        return []
    return [
        (
            node.lineno,
            f"`{statement}` in a hook module — hooks run in the agent host and operate on "
            "the graph `state` they are handed, never on the SDK",
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        and (statement := _imports_ava(node)) is not None
    ]


def _iter_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in targets:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    if argv:
        missing = [arg for arg in argv if not Path(arg).exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        targets = [Path(a).resolve() for a in argv]
    else:
        targets = lint_common.scan_roots(_REPO_ROOT, _SCAN_DIRS)

    scope = lint_common.changed_scope(only, _REPO_ROOT)

    total = 0
    for path in sorted(lint_common.restrict(_iter_files(targets), scope, _REPO_ROOT)):
        if lint_common.is_repo_test_file(path, _REPO_ROOT):
            continue
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, message in _scan_file(path, rel):
            total += 1
            print(f"{rel}:{lineno}: {message}")

    if total:
        print(
            f"\n{total} hook-module `ava` import(s). Read the graph `state` argument "
            "(`PluginStateHandle.view` / `.delta`), or move the SDK-touching code into a "
            "module that defines no hook; see scripts/lint/plugins/no_ava_in_hooks.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
