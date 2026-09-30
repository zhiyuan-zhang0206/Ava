"""A development tool that imports application code never touches a real home.

Importing application code boots the config package, which loads
`$AVA_HOME/.env` (else `~/.ava/.env`) — on a host that runs a cluster, that
cluster's credentials. A lint, a codegen dump or a docs check (every one of them
a pre-commit or pre-push hook's payload) therefore calls `enter_scratch_home()`
before its first application import: a fresh temporary `AVA_HOME`, whatever the
caller's environment carries.

The behaviour test runs a real tool with `AVA_HOME` and HOME pointing at homes
whose `.env` cannot be read, so any read of either one fails the tool loudly. The
structure test keeps the list closed: a tool that starts importing application
code without the call fails it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# Tool directories whose scripts are hook payloads or dev tools, and the one
# script outside them that is (the worktree-removal gate).
_TOOL_DIRS = ("scripts/lint", "scripts/content_lint", "scripts/codegen")
_TOOL_FILES = ("scripts/check_worktree_remove.py",)
_APPLICATION_PACKAGES = frozenset(
    {"base", "ava", "agent", "gateway", "cli", "ops", "services", "schedules", "ava_builtins"}
)

# Operator tools that act on a cluster's own data on purpose and therefore must
# resolve the real home: each carries the reason.
_OPERATOR_TOOLS = {
    "scripts/codegen/build_hierarchy_once.py": "builds one agent's history tree in the running cluster",
}


def _tool_scripts() -> list[Path]:
    found = [p for d in _TOOL_DIRS for p in sorted((_REPO / d).glob("*.py"))]
    found += [_REPO / f for f in _TOOL_FILES]
    return found


def _imports_application_code(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module]
        elif isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        else:
            continue
        if any(module.split(".")[0] in _APPLICATION_PACKAGES for module in modules):
            return True
    return False


def _calls_enter_scratch_home(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "enter_scratch_home"
        for node in ast.walk(tree)
    )


def test_every_tool_that_imports_application_code_enters_a_scratch_home() -> None:
    offenders: list[str] = []
    for script in _tool_scripts():
        rel = script.relative_to(_REPO).as_posix()
        tree = ast.parse(script.read_text(encoding="utf-8"))
        if not _imports_application_code(tree) or rel in _OPERATOR_TOOLS:
            continue
        if not _calls_enter_scratch_home(tree):
            offenders.append(rel)
    assert not offenders, (
        "these tools import application code without calling "
        f"`enter_scratch_home()` before their first application import: {offenders}"
    )


def test_the_operator_allowlist_names_only_tools_that_exist_and_import_application_code() -> None:
    """An allowlist entry that no longer applies is dead weight that would hide a
    future offender under its name."""
    for rel in _OPERATOR_TOOLS:
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        assert _imports_application_code(tree), rel


def _unreadable_home(root: Path) -> Path:
    """A home whose `.env` and `mirror.env` exist but cannot be opened."""
    home = root / "home-that-must-not-be-read"
    home.mkdir(parents=True)
    for name in (".env", "mirror.env"):
        path = home / name
        path.write_text("AVA_MACHINE_SERVE_GATEWAY=true\nAVA_CLUSTER_SECRET=canary\n")
        path.chmod(0)
    return home


def _clean_env(ava_home: Path, user_home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    env.update(AVA_HOME=str(ava_home), HOME=str(user_home))
    return env


def test_a_tool_never_reads_the_home_its_environment_names(tmp_path: Path) -> None:
    """The OpenAPI dump imports `gateway.app`: the heaviest import chain a hook
    payload runs. With `AVA_HOME` naming an unreadable home, and `~/.ava` under
    HOME unreadable too, it still succeeds — it never looked at either."""
    canary = _unreadable_home(tmp_path / "named")
    user_home = tmp_path / "user"
    (user_home / ".ava").mkdir(parents=True)
    for name in (".env", "mirror.env"):
        (user_home / ".ava" / name).write_text("AVA_CLUSTER_SECRET=canary\n")
        (user_home / ".ava" / name).chmod(0)
    out = tmp_path / "openapi.json"

    result = subprocess.run(  # noqa: S603 — fixed argv, repository tool, no shell
        [sys.executable, "scripts/codegen/dump_openapi.py", str(out)],
        cwd=_REPO,
        env=_clean_env(canary, user_home),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert out.is_file()
    assert sorted(p.name for p in canary.iterdir()) == [".env", "mirror.env"]
    assert sorted(p.name for p in (user_home / ".ava").iterdir()) == [".env", "mirror.env"]


def test_enter_scratch_home_overrides_the_callers_home_and_cleans_up(tmp_path: Path) -> None:
    """Whatever AVA_HOME the caller exported, the tool's tree gets a fresh private
    directory with the gateway fetch off, removed when the tool exits."""
    named = tmp_path / "named"
    named.mkdir()
    code = (
        "import os, sys\n"
        "from base.host.env.dotenv_boot import enter_scratch_home, resolve_ava_home\n"
        "home = enter_scratch_home()\n"
        "assert resolve_ava_home() == home != __import__('pathlib').Path(sys.argv[1])\n"
        "assert os.environ['AVA_CONFIG_FETCH'] == 'skip'\n"
        "assert oct(home.stat().st_mode & 0o777) == '0o700'\n"
        "print(home)\n"
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, literal probe
        [sys.executable, "-c", code, str(named)],
        cwd=_REPO,
        env=_clean_env(named, tmp_path / "user"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    scratch = Path(result.stdout.strip())
    assert not scratch.exists(), "the scratch home is removed at exit"
    assert scratch.name.startswith("ava-scratch-home-")
