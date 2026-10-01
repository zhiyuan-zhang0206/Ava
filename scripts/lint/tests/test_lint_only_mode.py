"""`--only FILE...` — the changed-files mode the commit hooks run the standalone lints in.

A hook that scans the whole repository on every commit costs seconds that grow with the
repository; `--only` makes the cost grow with the change instead. That is only safe if
three things hold, and each lint is held to all of them below:

- a changed file gets the verdict a full scan would give it (same scope, same exemptions);
- a file that did not change is not judged, so the run really is proportional to the change;
- an edit to the lint tooling (or to an input its rule reads) widens the run to the full
  scan, because it can change the verdict of files that did not move.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.lint import code_structure
from scripts.structure import lint_common

# A path every lint's tooling-edit check treats as "the lint itself changed".
_TOOLING = "scripts/lint/some_lint.py"


# ── the shared helpers ──────────────────────────────────────────────────────


def test_split_only_takes_the_rest_of_argv() -> None:
    assert lint_common.split_only(["a", "--only", "b.py", "c.py"]) == (["a"], ["b.py", "c.py"])
    assert lint_common.split_only(["a"]) == (["a"], None)
    assert lint_common.split_only(["--only"]) == ([], [])  # nothing changed is an answer


def test_changed_scope(tmp_path: Path) -> None:
    for rel in (
        "base/a.py",
        "scripts/lint/x.py",
        "scripts/lint/tests/test_x.py",
        "base/config/c.py",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n")
    assert lint_common.changed_scope(None, tmp_path) is None
    assert lint_common.changed_scope(["base/a.py"], tmp_path) == {"base/a.py"}
    assert lint_common.changed_scope([], tmp_path) == frozenset()
    # Tooling widens; the tooling's own tests do not.
    assert lint_common.changed_scope(["base/a.py", "scripts/lint/x.py"], tmp_path) is None
    assert lint_common.changed_scope(["scripts/lint/tests/test_x.py"], tmp_path) == {
        "scripts/lint/tests/test_x.py"
    }
    # A lint's own inputs widen too.
    assert (
        lint_common.changed_scope(["base/config/c.py"], tmp_path, inputs=("base/config/",)) is None
    )
    assert lint_common.changed_scope(["base/config/c.py"], tmp_path) == {"base/config/c.py"}


def test_changed_scope_rejects_a_path_that_does_not_exist(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"not found: typo\.py"):
        lint_common.changed_scope(["typo.py"], tmp_path)


def test_changed_scope_accepts_a_tracked_symlink_whose_target_is_gone(tmp_path: Path) -> None:
    """pre-commit hands a hook every tracked path that still exists as an entry, a dangling
    symlink included; `--all-files` must not die on one."""
    (tmp_path / "dangling.py").symlink_to(tmp_path / "missing.py")
    assert lint_common.changed_scope(["dangling.py"], tmp_path) == {"dangling.py"}


def test_restrict_keeps_only_the_changed_files(tmp_path: Path) -> None:
    paths = [tmp_path / "a.py", tmp_path / "b.py"]
    assert lint_common.restrict(paths, None, tmp_path) == paths
    assert lint_common.restrict(paths, frozenset({"b.py"}), tmp_path) == [tmp_path / "b.py"]


# ── every lint: the same verdict for a changed file, none for an unchanged one ───


@dataclass
class Case:
    module: str
    bad_path: str
    bad_source: str
    clean_path: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    patches: dict[str, object] = field(default_factory=dict)

    def clean(self) -> str:
        return self.clean_path or str(
            Path(self.bad_path).with_name("clean" + Path(self.bad_path).suffix)
        )


_EMOJI = chr(0x1F600)
_CJK = chr(0x4E2D)
_TAILNET_IP = ".".join(["100", "64", "1", "2"])  # spelled in pieces: the lint scans this file too

CASES = [
    Case("scripts.lint.no_emoji", "base/x.py", f"x = '{_EMOJI}'\n"),
    Case(
        "scripts.lint.logger_add_diagnose",
        "base/x.py",
        "from loguru import logger\nlogger.add('a.log')\n",
    ),
    Case(
        "scripts.lint.loguru_format",
        "base/x.py",
        "from loguru import logger\nlogger.warning('x %s', 1)\n",
    ),
    Case(
        "scripts.lint.termination_source",
        "base/x.py",
        "cur.execute(\"UPDATE agents_meta SET status = 'terminated' WHERE id = %s\", (a,))\n",
        patches={"_termination_source_values": lambda: frozenset({"launch-confirm"})},
    ),
    Case("scripts.lint.no_os_environ", "base/x.py", "import os\nx = os.environ['A']\n"),
    Case(
        "scripts.lint.turn_scoped_config",
        "agent/x.py",
        "from base.config import settings\nx = settings.agent.checkpoint_interval\n",
    ),
    Case(
        "scripts.lint.clock_lattice",
        "ops/x.py",
        "_SOME_REAP_GRACE_S = 100.0\n",
        patches={"_stale_allowlist_entries": list},
    ),
    Case(
        "scripts.lint.no_plugin_wrap",
        "ava_builtins/plugins/demo/plugin.py",
        "ava.files.read = my_read\n",
    ),
    Case(
        "scripts.lint_pool_keepalives",
        "scripts/x.py",
        "pool = ConnectionPool(url, min_size=1, max_size=2, open=True)\n",
    ),
    Case(
        "scripts.lint.fixture_scope",
        "tests/sub/conftest.py",
        '@pytest.fixture(scope="session")\ndef _unset():\n    os.environ.pop("AVA_HOME", None)\n    yield\n',
    ),
    Case(
        "scripts.lint.no_script_sibling_imports",
        "tools/runner.py",
        "from helper import VALUE\n\n\ndef main() -> int:\n    return 0\n\n\n"
        'if __name__ == "__main__":\n    main()\n',
        extra={"tools/helper.py": "VALUE = 1\n"},
    ),
    Case(
        "scripts.lint.async_no_sync_blocking",
        "gateway/x.py",
        "import time\n\n\nasync def f():\n    time.sleep(1)\n",
        patches={"_stale_repo_helpers": list},
    ),
    Case("scripts.lint.time_bomb", "tests/test_a.py", "def test_x():\n    f(since='2026-09-06')\n"),
    Case("scripts.content_lint.lint_no_cjk", "docs/a.md", f"{_CJK}\n"),
    Case("scripts.content_lint.lint_no_tailnet", "docs/a.md", f"see {_TAILNET_IP}\n"),
]


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    for rel in (*lint_common.FRAMEWORK_DIRS, "scripts", "tests", "base/lm", "ava_builtins/plugins"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.module.rsplit(".", 1)[-1])
def test_only_judges_exactly_the_changed_files(
    case: Case, fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lint = importlib.import_module(case.module)
    for root_name in ("_REPO_ROOT", "_ROOT"):
        if hasattr(lint, root_name):
            monkeypatch.setattr(lint, root_name, fake_repo)
    for name, value in case.patches.items():
        monkeypatch.setattr(lint, name, value)
    if hasattr(lint, "_tracked_files"):  # a scratch tree is not a git repository
        monkeypatch.setattr(
            lint,
            "_tracked_files",
            lambda: sorted(
                p.relative_to(fake_repo).as_posix() for p in fake_repo.rglob("*") if p.is_file()
            ),
        )
    _write(fake_repo, case.bad_path, case.bad_source)
    _write(fake_repo, case.clean(), "x = 1\n")
    for rel, text in case.extra.items():
        _write(fake_repo, rel, text)
    _write(fake_repo, _TOOLING, "")

    main: Callable[[list[str]], int] = lint.main
    assert main([]) == 1, "the sample must be a violation the full scan reports"
    assert main(["--only", case.bad_path]) == 1, "a changed file gets the full scan's verdict"
    assert main(["--only", case.clean()]) == 0, "a file that did not change is not judged"
    assert main(["--only", case.clean(), _TOOLING]) == 1, "a tooling edit widens to the full scan"
    assert main(["--only"]) == 0, "nothing changed, nothing to judge"


# ── the lints with their own `--only` plumbing ──────────────────────────────


def test_code_structure_turns_changed_files_into_explicit_targets(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    (tmp_path / "base/a.py").write_text("x = 1\n")
    (tmp_path / "scripts/structure").mkdir(parents=True)
    (tmp_path / "scripts/structure/baseline_shards.py").write_text("x = 1\n")
    original = code_structure._REPO_ROOT
    try:
        code_structure._REPO_ROOT = tmp_path
        assert code_structure._changed_targets(None) == []
        assert code_structure._changed_targets(["base/a.py"]) == [str(tmp_path / "base/a.py")]
        # Tooling or baseline: no explicit targets, so the lint scans everything.
        assert code_structure._changed_targets(["scripts/structure/baseline_shards.py"]) == []
    finally:
        code_structure._REPO_ROOT = original
    assert code_structure._parse_args(["--only"]) == ([], False, True)
    assert code_structure._parse_args(["--complexity-warnings-full"]) == ([], True, False)


def test_zombie_ignores_checks_only_the_changed_python_files(tmp_path: Path) -> None:
    from scripts.lint import zombie_pyright_ignores as zombie

    for rel in ("a.py", "notes.md", "pyproject.toml"):
        (tmp_path / rel).write_text("x\n")
    original = zombie._REPO_ROOT
    try:
        zombie._REPO_ROOT = tmp_path
        assert zombie._changed_python(None) is None
        assert zombie._changed_python(["a.py", "notes.md"]) == ["a.py"]
        # The tier configuration the ignores are judged against is an input.
        assert zombie._changed_python(["a.py", "pyproject.toml"]) is None
    finally:
        zombie._REPO_ROOT = original


def test_code_structure_checks_every_ancestor_directory_of_a_changed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new subpackage adds an entry to each ancestor's direct-entry budget, so a changed
    file checks every directory from its own up to its scope root, not just its parent."""
    monkeypatch.setattr(code_structure, "_REPO_ROOT", tmp_path)
    module = tmp_path / "base/pkg/sub/mod.py"
    module.parent.mkdir(parents=True)
    module.write_text("x = 1\n")
    files, directories = code_structure._budget_targets([module])
    assert files == {module}
    assert directories == {tmp_path / "base", tmp_path / "base/pkg", tmp_path / "base/pkg/sub"}


def test_a_module_is_built_when_first_reached_and_only_once() -> None:
    from scripts.structure.lazy_modules import ModuleMap

    built: list[str] = []

    def build(name: str) -> str | None:
        built.append(name)
        return None if name == "empty" else name.upper()

    modules = ModuleMap(lambda name: name in {"a", "empty"}, build)
    assert "a" in modules and "missing" not in modules
    assert built == [], "asking whether a module exists must not build it"
    assert modules.get("a") == "A" and modules["a"] == "A"
    assert built == ["a"], "a module is built once"
    assert modules.get("missing", "default") == "default"
    assert modules.get("empty", "default") == "default"
    with pytest.raises(KeyError):
        modules["empty"]
