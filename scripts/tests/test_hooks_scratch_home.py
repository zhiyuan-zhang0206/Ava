"""The git hooks run in a scratch home.

pre-commit and pre-push run the repository's own scripts on every commit and push; nobody
chose to run them, and a developer's checkout often sits on a host that runs a cluster.
Importing application code boots the config package, which loads `$AVA_HOME/.env` (else
`~/.ava/.env`): that cluster's credentials. So every script a hook launches that reaches
application code calls `enter_scratch_home()` — a fresh temporary `AVA_HOME`, whatever the
caller's environment carries — behind `if __name__ == "__main__":`, before its first
import that reaches application code. Only as a program: tests import these modules, and a
module that entered a scratch home while being imported would replace the whole pytest
worker's `AVA_HOME` (`enter_scratch_home` refuses inside pytest for that reason).

Every other script is run by a person or an agent, on purpose. For those the rule is the
development convention (`docs/conventions/dev-setup.md`), not code.

The hook scripts are derived from `.pre-commit-config.yaml`, so a new hook is covered
without anyone remembering this file. One canary run proves the mechanism end to end.
"""

from __future__ import annotations

import ast
import functools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_APPLICATION_PACKAGES = frozenset(
    {"base", "ava", "agent", "gateway", "cli", "ops", "services", "schedules", "ava_builtins"}
)
_SCRATCH_PROVIDER = "base.host.env.dotenv_boot"

# A hook script that reaches application code and still needs no scratch home.
_BOOTS_NOTHING = {
    "scripts/lint/locks/python_lock.py": (
        "CI runs it on a bare python3, and its one application import "
        "(`base.deploy.release.python_lock`) is stdlib-only: it can never reach the config boot"
    ),
}

_PYTHON_ENTRY = re.compile(r"(?:python3?|\.venv/bin/python)\s+(scripts/[\w./-]+\.py)")
_SHELL_ENTRY = re.compile(r"(scripts/[\w./-]+\.sh)")


@functools.cache
def _hook_scripts() -> tuple[str, ...]:
    """Every `scripts/*.py` a hook launches: named in an `entry:`, or run by a shell
    script an `entry:` names."""
    config = (_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    found: set[str] = set()
    for entry in re.findall(r"^\s*entry:\s+(.+)$", config, re.MULTILINE):
        found.update(_PYTHON_ENTRY.findall(entry))
        for shell in _SHELL_ENTRY.findall(entry):
            found.update(_PYTHON_ENTRY.findall((_REPO / shell).read_text(encoding="utf-8")))
    return tuple(sorted(found))


@functools.cache
def _tree(rel: str) -> ast.Module:
    return ast.parse((_REPO / rel).read_text(encoding="utf-8"))


def _imported_modules(node: ast.AST) -> list[str]:
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return []


def _repo_script_module(module: str) -> str | None:
    """The file of a `scripts.*` module, repo-relative."""
    if module.split(".", maxsplit=1)[0] != "scripts":
        return None
    path = _REPO.joinpath(*module.split("."))
    for candidate in (path.with_suffix(".py"), path / "__init__.py"):
        if candidate.is_file():
            return candidate.relative_to(_REPO).as_posix()
    return None


def _module_reaches_application_code(module: str, seen: set[str]) -> bool:
    """Whether importing `module` loads application code: it is one (the scratch-home
    provider excepted), or a `scripts.*` helper whose own imports reach one."""
    if module == _SCRATCH_PROVIDER or module.startswith(_SCRATCH_PROVIDER + "."):
        return False
    if module.split(".", maxsplit=1)[0] in _APPLICATION_PACKAGES:
        return True
    rel = _repo_script_module(module)
    if rel is None or rel in seen:
        return False
    seen.add(rel)
    return any(
        _module_reaches_application_code(name, seen)
        for node in ast.walk(_tree(rel))
        for name in _imported_modules(node)
    )


def _reaches_application_code(rel: str) -> bool:
    seen = {rel}
    return any(
        _module_reaches_application_code(name, seen)
        for node in ast.walk(_tree(rel))
        for name in _imported_modules(node)
    )


def _first_reaching_import_line(rel: str) -> int | None:
    """First module-level import of `rel` that loads application code."""
    for node in _tree(rel).body:
        if any(_module_reaches_application_code(name, {rel}) for name in _imported_modules(node)):
            return node.lineno
    return None


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


def _guarded_call_line(rel: str) -> int | None:
    """Line of the module-level `if __name__ == "__main__":` that calls it."""
    for node in _tree(rel).body:
        if _is_main_guard(node) and any(_is_enter_call(s) for s in getattr(node, "body", [])):
            return node.lineno
    return None


def test_every_hook_script_that_reaches_application_code_enters_a_scratch_home() -> None:
    hooks = _hook_scripts()
    # A scan that silently found nothing would make this test vacuous.
    assert "scripts/lint/no_os_environ.py" in hooks
    assert "scripts/codegen/dump_openapi.py" in hooks  # reached through a shell entry
    problems: list[str] = []
    for rel in hooks:
        if rel in _BOOTS_NOTHING or not _reaches_application_code(rel):
            continue
        guard = _guarded_call_line(rel)
        if guard is None:
            problems.append(f'{rel}: no `if __name__ == "__main__": enter_scratch_home()`')
            continue
        first = _first_reaching_import_line(rel)
        if first is not None and first < guard:
            problems.append(f"{rel}: loads application code (line {first}) before the call")
    assert not problems, (
        "a hook script that reaches application code must call `enter_scratch_home()` behind "
        f'`if __name__ == "__main__":`, before that import: {problems}'
    )


@pytest.mark.parametrize("rel", sorted(_BOOTS_NOTHING))
def test_a_hook_script_exempt_from_the_scratch_home_runs_without_a_third_party_package(
    rel: str, tmp_path: Path
) -> None:
    """`python -S` drops site-packages, which a runner's own `python3` lacks: the script's
    module body must run with the standard library alone, so it cannot reach the config
    boot that the scratch home exists to keep away from a real home."""
    assert rel in _hook_scripts()
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    env["HOME"] = str(tmp_path)
    result = subprocess.run(  # noqa: S603 — fixed argv, repository script, no shell
        [
            sys.executable,
            "-S",
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1], run_name='bare_import_check')",
            rel,
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


def test_a_hook_script_never_reads_the_home_its_environment_names(tmp_path: Path) -> None:
    """The canary. The OpenAPI dump (a hook's payload) imports `gateway.app`, the heaviest
    import chain a hook runs. With `AVA_HOME` naming an unreadable home and `~/.ava` under
    HOME unreadable too, it still succeeds: it never looked at either."""
    canary = _unreadable_home(tmp_path / "named")
    user_home = tmp_path / "user"
    (user_home / ".ava").mkdir(parents=True)
    for name in (".env", "mirror.env"):
        (user_home / ".ava" / name).write_text("AVA_CLUSTER_SECRET=canary\n")
        (user_home / ".ava" / name).chmod(0)
    out = tmp_path / "openapi.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    env.update(AVA_HOME=str(canary), HOME=str(user_home))

    result = subprocess.run(  # noqa: S603 — fixed argv, repository tool, no shell
        [sys.executable, "scripts/codegen/dump_openapi.py", str(out)],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert out.is_file()
    assert sorted(p.name for p in canary.iterdir()) == [".env", "mirror.env"]
    assert sorted(p.name for p in (user_home / ".ava").iterdir()) == [".env", "mirror.env"]
