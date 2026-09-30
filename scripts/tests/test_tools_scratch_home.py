"""A script that imports application code enters a scratch home, or says what it operates on.

Importing application code boots the config package, which loads
`$AVA_HOME/.env` (else `~/.ava/.env`) — on a host that runs a cluster, that
cluster's credentials. Every entry file under `scripts/`, `.agents/skills/` and
`ava_builtins/skills/` that imports application code therefore takes one of two
positions, and the structure test names the third as a failure:

- a development or CI tool calls `enter_scratch_home()` — a fresh temporary
  `AVA_HOME`, whatever the caller's environment carries — behind
  `if __name__ == "__main__":`, before its first application import. Only as a
  program: tests import these modules, and a module that entered a scratch home
  while being imported would replace the whole pytest worker's `AVA_HOME`;
- a tool that really operates on the local cluster declares it, with the kind of
  state it touches and why: a line `# operates-on-cluster: <states> -- <reason>` in
  the file, or one row of `_CLUSTER_OPERATOR_DIRS` for a whole directory of them.
  The states come from a closed vocabulary, so a reviewer sees at a glance whether a
  tool reads a database, a secret or a PTY registry.

An entry file is one with an `if __name__ == "__main__":` guard, module-level code
that does work (a watcher script), or a target named by a workflow or a hook. A library
(defs and imports only) needs neither, but it must not enter a scratch home nor resolve
the home when it is imported: the entry that imports it owns that decision.

The behaviour test runs a real tool with `AVA_HOME` and HOME pointing at homes
whose `.env` cannot be read, so any read of either one fails the tool loudly. The
import test executes every scratch-home tool module inside the pytest process and
asserts the session's `AVA_HOME` is unchanged.

The tools CI and the hooks run on a bare `python3` (no project dependencies
installed) are the exception: they cannot reach the config boot at all, because
`enter_scratch_home` lives where `dotenv` is imported. They need no scratch home, and
a test runs each one under `python -S` to prove it stays that way.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# The script roots the rule covers. Tests, `tests/` directories and conftests are
# not tools.
_SCAN_ROOTS = ("scripts", ".agents/skills", "ava_builtins/skills")
_APPLICATION_PACKAGES = frozenset(
    {"base", "ava", "agent", "gateway", "cli", "ops", "services", "schedules", "ava_builtins"}
)

# What an operator tool may say it operates on: the local cluster's state, by kind.
_CLUSTER_STATES = frozenset(
    {
        "agent-records",  # agents, their messages and schedules (the database rows and the gateway)
        "backups",  # the logical dumps, WAL archive and restore targets
        "config",  # the cluster's configuration and the enabled plugins
        "database",  # the cluster's Postgres
        "home-files",  # files under the home: caches, snapshots, workspaces
        "host-services",  # this machine's daemons: the permissions helper, launchd, the gateway process
        "memory",  # the shared memory pool
        "pty-sessions",  # the PTY session registry
        "secrets",  # the cluster secret, API tokens, data-plane passwords
        "skills",  # installed skills and their identity
        "telemetry",  # logs, traces and metrics the cluster keeps
    }
)

# A whole directory of operator tools: every entry file under it operates on the local
# cluster. Each carries the states it touches and why.
_CLUSTER_OPERATOR_DIRS: dict[str, tuple[tuple[str, ...], str]] = {
    "ava_builtins/skills": (
        ("agent-records", "config", "database", "memory", "pty-sessions", "skills"),
        "run by a fleet agent in its own production shell through the `ava` SDK, acting on the "
        "running cluster's agents, messages, memory pool, skills and schedules",
    ),
    "scripts/data_plane_ops": (
        ("backups", "database", "secrets"),
        "operator procedures on this cluster's Postgres, Redis and their secrets: rotation, "
        "restore drills, PITR migration",
    ),
    "scripts/data_repair": (
        ("database", "memory", "skills", "telemetry"),
        "one-shot repairs of this cluster's rows, memory index, skill identities and Loki lineage",
    ),
    "scripts/host_ops": (
        ("database", "host-services"),
        "host-level operations: the hosted-inbound backlog sweep and the launchd permissions-"
        "helper fault harnesses, which use this machine's launchd domain with a test label",
    ),
}

# An individual declaration: one comment line in the file, before its first statement
# that is not the docstring.
_DECLARATION = re.compile(
    r"^# operates-on-cluster: (?P<states>[a-z-]+(?:, [a-z-]+)*) -- (?P<reason>\S.*)$", re.MULTILINE
)

# `python3 scripts/...` (or `ui/...`) on a line that is not `uv run`: CI steps and
# pre-commit entries that run on the interpreter the runner ships with.
_BARE_ENTRY = re.compile(r"(?:^|[\s:])python3?\s+((?:scripts|ui)/[\w./-]+\.py)")
# Any script a workflow or a hook launches, whatever the interpreter wrapper.
_LAUNCHED_ENTRY = re.compile(
    r"(?:python3?|\.venv/bin/python)\s+((?:scripts|\.agents/skills|ava_builtins/skills)/[\w./-]+\.py)"
)


def _workflow_sources() -> list[Path]:
    return [
        *sorted((_REPO / ".github" / "workflows").glob("*.yml")),
        _REPO / ".pre-commit-config.yaml",
    ]


def _bare_python_scripts() -> list[str]:
    found: set[str] = set()
    for source in _workflow_sources():
        for line in source.read_text(encoding="utf-8").splitlines():
            if "uv run" not in line:
                found.update(_BARE_ENTRY.findall(line))
    return sorted(found)


def _launched_scripts() -> set[str]:
    return {
        match
        for source in _workflow_sources()
        for match in _LAUNCHED_ENTRY.findall(source.read_text(encoding="utf-8"))
    }


def _is_tool_path(path: Path) -> bool:
    rel = path.relative_to(_REPO)
    skipped = {"tests", "__pycache__", "node_modules", ".venv"}
    # A test is a `test_*.py` inside a `tests/` directory (pyproject `python_files`): where a
    # file sits, not its name, makes it one (`scripts/ci/test_selector.py` is a CI tool).
    return not (skipped & set(rel.parts) or path.name == "conftest.py")


@functools.cache
def _scan_files() -> tuple[str, ...]:
    """Every Python file under the scan roots that is a tool (not a test)."""
    return tuple(
        sorted(
            path.relative_to(_REPO).as_posix()
            for root in _SCAN_ROOTS
            for path in (_REPO / root).rglob("*.py")
            if _is_tool_path(path)
        )
    )


@functools.cache
def _tree(rel: str) -> ast.Module:
    return ast.parse((_REPO / rel).read_text(encoding="utf-8"))


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


def _executes_at_import(tree: ast.Module) -> bool:
    """Whether the module body does work when imported: a call statement, a loop, a
    `with`, a `raise`. Imports, definitions, assignments and the `sys.path` set-up a
    script does before its imports do not count."""

    def is_path_setup(node: ast.Expr) -> bool:
        return isinstance(node.value, ast.Call) and ast.unparse(node.value.func).startswith(
            "sys.path."
        )

    def works(stmts: list[ast.stmt]) -> bool:
        for node in stmts:
            if isinstance(node, ast.Expr):
                if not isinstance(node.value, ast.Constant) and not is_path_setup(node):
                    return True
            elif isinstance(node, ast.For | ast.AsyncFor | ast.While | ast.With | ast.Raise):
                return True
            elif isinstance(node, ast.Try):
                handler_bodies = [h.body for h in node.handlers]
                bodies = [node.body, node.orelse, node.finalbody, *handler_bodies]
                if any(works(body) for body in bodies):
                    return True
            elif (
                isinstance(node, ast.If)
                and not _is_main_guard(node)
                and (works(node.body) or works(node.orelse))
            ):
                return True
        return False

    return works(tree.body)


def _is_entry(rel: str, tree: ast.Module, launched: set[str]) -> bool:
    return (
        any(_is_main_guard(node) for node in tree.body)
        or _executes_at_import(tree)
        or rel in launched
    )


@functools.cache
def _home_resolvers() -> frozenset[str]:
    """Every name that resolves the home (or a path under it): the functions of
    `base.paths` and the resolver behind them."""
    tree = ast.parse((_REPO / "base" / "paths" / "__init__.py").read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    return frozenset(names | {"resolve_ava_home"})


def _resolves_home_on_import(tree: ast.Module) -> list[int]:
    """Lines of calls, made when the module is imported, to a home resolver."""
    lines: list[int] = []
    resolvers = set(_home_resolvers())
    resolvers |= {  # `from base.paths import ava_home as home`
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.asname and alias.name in _home_resolvers()
    }

    class Visitor(ast.NodeVisitor):
        # A function or lambda body runs when it is called, not when the module is imported.
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return None

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return None

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return None

        def visit_If(self, node: ast.If) -> None:
            if not _is_main_guard(node):  # the guard body runs only as a program
                self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in resolvers:
                lines.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    return lines


def _declaration(rel: str, problems: list[str]) -> tuple[tuple[str, ...], str] | None:
    """What `rel` declares it operates on: its own comment, or its directory's row."""
    found = _DECLARATION.findall((_REPO / rel).read_text(encoding="utf-8"))
    by_dir = [
        row for directory, row in _CLUSTER_OPERATOR_DIRS.items() if rel.startswith(directory + "/")
    ]
    if len(found) > 1 or (found and by_dir):
        problems.append(f"{rel}: declares what it operates on more than once")
    if found:
        states, reason = found[0]
        declared = (tuple(states.split(", ")), reason)
    elif by_dir:
        declared = by_dir[0]
    else:
        return None
    unknown = set(declared[0]) - _CLUSTER_STATES
    if unknown:
        problems.append(f"{rel}: states {sorted(unknown)} are not in the vocabulary")
    if len(declared[1]) < 15:
        problems.append(f"{rel}: the reason is too short to be a reason")
    return declared


def _scripts_needing_a_decision() -> tuple[str, ...]:
    bare = set(_bare_python_scripts())
    return tuple(
        rel for rel in _scan_files() if rel not in bare and _imports_application_code(_tree(rel))
    )


def test_every_entry_that_imports_application_code_takes_a_position() -> None:
    """Enter a scratch home when run, or declare the local cluster state it operates on."""
    problems: list[str] = []
    launched = _launched_scripts()
    for rel in _scripts_needing_a_decision():
        tree = _tree(rel)
        if not _is_entry(rel, tree, launched):
            continue
        guard = _guarded_call_line(tree)
        declared = _declaration(rel, problems)
        if guard is None and declared is None:
            problems.append(
                f'{rel}: no `if __name__ == "__main__": enter_scratch_home()` and no '
                "`# operates-on-cluster: <states> -- <reason>` declaration"
            )
        elif guard is not None and declared is not None:
            problems.append(f"{rel}: enters a scratch home AND declares it operates on the cluster")
        elif guard is not None:
            first_import = _first_application_import_line(tree)
            if first_import is not None and first_import < guard:
                problems.append(f"{rel}: imports application code (line {first_import}) first")
    assert not problems, "\n" + "\n".join(problems)


def test_a_library_neither_enters_a_scratch_home_nor_resolves_the_home_on_import() -> None:
    """A module that other scripts import leaves the choice to the entry that imports it."""
    problems: list[str] = []
    launched = _launched_scripts()
    for rel in _scripts_needing_a_decision():
        tree = _tree(rel)
        if _is_entry(rel, tree, launched):
            continue
        if _calls_enter_scratch_home(tree):
            problems.append(f"{rel}: a library calls enter_scratch_home()")
        for line in _resolves_home_on_import(tree):
            problems.append(f"{rel}:{line}: a library resolves the home when it is imported")
    assert not problems, "\n" + "\n".join(problems)


def test_the_declarations_name_entries_that_exist_and_need_them() -> None:
    """A declaration that no longer applies would hide a future offender under its name."""
    problems: list[str] = []
    launched = _launched_scripts()
    entries = [rel for rel in _scripts_needing_a_decision() if _is_entry(rel, _tree(rel), launched)]
    for directory, (states, reason) in _CLUSTER_OPERATOR_DIRS.items():
        if not [rel for rel in entries if rel.startswith(directory + "/")]:
            problems.append(
                f"{directory}: declared, but no entry under it imports application code"
            )
        if set(states) - _CLUSTER_STATES or len(reason) < 15:
            problems.append(f"{directory}: malformed declaration")
    for rel in _scan_files():
        text = (_REPO / rel).read_text(encoding="utf-8")
        if _DECLARATION.search(text) and rel not in entries:
            problems.append(f"{rel}: declares what it operates on but is not such an entry")
    assert not problems, "\n" + "\n".join(problems)


def test_the_scan_covers_the_roots_it_is_meant_to_cover() -> None:
    """A scan that silently matched little would make the tests above vacuous."""
    needing = set(_scripts_needing_a_decision())
    for rel in (
        "scripts/lint/no_os_environ.py",  # enters a scratch home
        "scripts/ci/release_cut.py",  # enters a scratch home
        "scripts/start_gateway.py",  # declares
        ".agents/skills/inspect-a-trace/scripts/fetch_trace.py",  # declares
        "ava_builtins/skills/ava-watcher/scripts/watch_idle.py",  # its directory declares
        "ava_builtins/skills/web-ai/scripts/_utils.py",  # a library
    ):
        assert rel in needing, rel
    assert len(needing) >= 60


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
    return [rel for rel in _scan_files() if _calls_enter_scratch_home(_tree(rel))]


def test_the_import_test_covers_the_tools_it_is_meant_to_cover() -> None:
    tools = _scratch_home_tools()
    assert len(tools) >= 22
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
