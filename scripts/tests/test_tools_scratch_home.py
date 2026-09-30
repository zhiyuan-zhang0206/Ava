"""A development tool that imports application code never touches a real home.

Importing application code boots the config package, which loads
`$AVA_HOME/.env` (else `~/.ava/.env`) — on a host that runs a cluster, that
cluster's credentials. A lint, a codegen dump or a docs check (every one of them
a pre-commit or pre-push hook's payload) therefore calls `enter_scratch_home()`
before its first application import: a fresh temporary `AVA_HOME`, whatever the
caller's environment carries. It does so only when it runs as a program, behind
`if __name__ == "__main__":`: tests import these modules, and a module that
entered a scratch home while being imported would replace the whole pytest
worker's `AVA_HOME`.

The behaviour test runs a real tool with `AVA_HOME` and HOME pointing at homes
whose `.env` cannot be read, so any read of either one fails the tool loudly. The
structure test keeps the list closed: a tool that starts importing application
code without the guarded call fails it. The import test executes every such tool
module inside the pytest process and asserts the session's `AVA_HOME` is unchanged.

The opposite rule holds for the tools CI and the hooks run on a bare `python3`
(no project dependencies installed): they cannot reach the config boot at all,
because `enter_scratch_home` lives where `dotenv` is imported. They need no
scratch home, and a test runs each one under `python -S` to prove it stays that
way.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

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
    "scripts/check_worktree_remove.py": (
        "reads this machine's session registry ($AVA_HOME/run/pty) to find the live anchors "
        "of a worktree, so it must read the real home; it only skips the gateway config fetch "
        "(tests/base/test_worktree_guard.py proves it dials nothing and writes nothing)"
    ),
}


# `python3 scripts/...` (or `ui/...`) on a line that is not `uv run`: CI steps and
# pre-commit entries that run on the interpreter the runner ships with.
_BARE_ENTRY = re.compile(r"(?:^|[\s:])python3?\s+((?:scripts|ui)/[\w./-]+\.py)")


def _bare_python_scripts() -> list[str]:
    sources = [
        *sorted((_REPO / ".github" / "workflows").glob("*.yml")),
        _REPO / ".pre-commit-config.yaml",
    ]
    found: set[str] = set()
    for source in sources:
        for line in source.read_text(encoding="utf-8").splitlines():
            if "uv run" not in line:
                found.update(_BARE_ENTRY.findall(line))
    return sorted(found)


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


def _is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


def _is_enter_call(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "enter_scratch_home"
    )


def _guarded_call_line(tree: ast.Module) -> int | None:
    """Line of the module-level `if __name__ == "__main__":` that calls it."""
    for node in tree.body:
        if _is_main_guard(node) and any(_is_enter_call(s) for s in getattr(node, "body", [])):
            return node.lineno
    return None


def _calls_enter_scratch_home(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "enter_scratch_home"
        for node in ast.walk(tree)
    )


def _first_application_import_line(tree: ast.Module) -> int | None:
    """First module-level application import other than the one that provides the call."""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module]
        elif isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        else:
            continue
        if modules == ["base.host.env.dotenv_boot"]:
            continue
        if any(module.split(".")[0] in _APPLICATION_PACKAGES for module in modules):
            return node.lineno
    return None


def test_every_tool_that_imports_application_code_enters_a_scratch_home_when_run() -> None:
    offenders: list[str] = []
    bare = set(_bare_python_scripts())
    for script in _tool_scripts():
        rel = script.relative_to(_REPO).as_posix()
        tree = ast.parse(script.read_text(encoding="utf-8"))
        if not _imports_application_code(tree) or rel in _OPERATOR_TOOLS or rel in bare:
            continue
        guard = _guarded_call_line(tree)
        if guard is None:
            offenders.append(f'{rel}: no `if __name__ == "__main__": enter_scratch_home()`')
            continue
        first_import = _first_application_import_line(tree)
        if first_import is not None and first_import < guard:
            offenders.append(f"{rel}: imports application code (line {first_import}) first")
    assert not offenders, (
        "every tool that imports application code must call `enter_scratch_home()` behind "
        f'`if __name__ == "__main__":`, before its first application import: {offenders}'
    )


def test_the_operator_allowlist_names_only_tools_that_exist_and_import_application_code() -> None:
    """An allowlist entry that no longer applies is dead weight that would hide a
    future offender under its name."""
    for rel in _OPERATOR_TOOLS:
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        assert _imports_application_code(tree), rel


def test_the_bare_python_scan_finds_the_entries_it_is_meant_to_guard() -> None:
    """A scan that silently matched nothing would make the next test vacuous."""
    bare = _bare_python_scripts()
    assert "scripts/lint/python_lock.py" in bare
    assert "scripts/content_lint/lint_no_cjk.py" in bare
    assert "scripts/provision/check_git_hooks.py" in bare


@pytest.mark.parametrize("script", _bare_python_scripts())
def test_a_tool_run_on_a_bare_python3_imports_without_a_third_party_package(
    script: str, tmp_path: Path
) -> None:
    """`python -S` drops site-packages, which is what a runner's own `python3`
    lacks: the tool's module body (its imports, not its `main()`) must run without
    any project dependency, so it can neither call `enter_scratch_home` nor reach
    the config boot. HOME is a temporary directory: a stray home read or write
    cannot touch the operator's."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    env["HOME"] = str(tmp_path)
    result = subprocess.run(  # noqa: S603 — fixed argv, repository tool, no shell
        [
            sys.executable,
            "-S",
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1], run_name='bare_import_check')",
            script,
        ],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".ava").exists()


def test_the_ci_classify_step_imports_with_the_standard_library_alone(tmp_path: Path) -> None:
    """`ci.yml`'s classify step runs `from base.deploy.git.repo_change import
    classify_change` on the runner's bare `python3`."""
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import sys; sys.path.insert(0, '.'); "
            "from base.deploy.git.repo_change import classify_change",
        ],
        cwd=_REPO,
        env={"HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _scratch_home_tools() -> list[str]:
    return [
        script.relative_to(_REPO).as_posix()
        for script in _tool_scripts()
        if _calls_enter_scratch_home(ast.parse(script.read_text(encoding="utf-8")))
    ]


def test_the_import_test_covers_the_tools_it_is_meant_to_cover() -> None:
    tools = _scratch_home_tools()
    assert len(tools) >= 17
    assert "scripts/lint/no_os_environ.py" in tools
    assert "scripts/content_lint/lint_ava_okf.py" in tools


@pytest.mark.parametrize("rel", _scratch_home_tools())
def test_importing_a_tool_leaves_the_sessions_home_alone(
    rel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests import these modules (the lints' own tests do, at collection). The body
    is executed afresh under a probe name, so a cached import cannot hide a module
    that enters a scratch home at import time; such a module would replace the
    worker's `AVA_HOME` and every later test would read the wrong home."""
    home = os.environ["AVA_HOME"]
    fetch = os.environ.get("AVA_CONFIG_FETCH")
    monkeypatch.setenv("AVA_HOME", home)  # restored on teardown whatever the module does
    name = "_tool_import_probe_" + Path(rel).stem
    spec = importlib.util.spec_from_file_location(name, _REPO / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    assert os.environ["AVA_HOME"] == home
    assert os.environ.get("AVA_CONFIG_FETCH") == fetch


def test_enter_scratch_home_refuses_inside_a_pytest_process() -> None:
    from base.host.env.dotenv_boot import enter_scratch_home

    home = os.environ["AVA_HOME"]
    with pytest.raises(RuntimeError, match="pytest process"):
        enter_scratch_home()
    assert os.environ["AVA_HOME"] == home


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
