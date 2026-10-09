"""`scripts/lint/plugins/no_ava_in_hooks.py` — hook modules may not import `ava`, in any form."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint.plugins import no_ava_in_hooks as gate


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    for scan_dir in gate._SCAN_DIRS:
        (tmp_path / scan_dir).mkdir()
    return tmp_path


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "statement",
    [
        "import ava",
        "import ava.sdk_surface.agent_identity as ident",
        "from ava import skills",
        "from ava.sdk_surface import install",
    ],
)
def test_ava_import_in_a_hook_subclass_module_is_reported(
    repo: Path, capsys: pytest.CaptureFixture[str], statement: str
) -> None:
    _write(repo, "ava_builtins/plugins/p/hook.py", f"{statement}\nclass H(Hook):\n    pass\n")
    assert gate.main([]) == 1
    assert "ava_builtins/plugins/p/hook.py:1:" in capsys.readouterr().out


def test_deferred_and_type_checking_imports_are_reported(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        "ava_builtins/plugins/p/agent_runtime.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import ava\n"
        "def f():\n"
        "    from ava.skills import identifier\n",
    )
    assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "agent_runtime.py:3:" in out
    assert "agent_runtime.py:5:" in out


def test_hooks_package_module_is_a_hook_module_without_a_subclass(repo: Path) -> None:
    _write(repo, "agent/hooks/helper.py", "import ava\n")
    assert gate.main([]) == 1


def test_dotted_hook_base_is_recognised(repo: Path) -> None:
    _write(repo, "demos/d/gate.py", "import ava\nclass G(hooks.Hook):\n    pass\n")
    assert gate.main([]) == 1


def test_non_hook_module_and_tests_may_import_ava(repo: Path) -> None:
    _write(repo, "ava_builtins/plugins/p/plugin.py", "import ava\n")
    _write(repo, "agent/graph/capabilities.py", "def f():\n    import ava\n")
    _write(repo, "agent/hooks/tests/test_x.py", "import ava\nclass H(Hook):\n    pass\n")
    assert gate.main([]) == 0


def test_hook_module_without_ava_import_passes(repo: Path) -> None:
    _write(
        repo,
        "ava_builtins/plugins/p/agent_runtime.py",
        "from . import plugin\nfrom agent.hooks import Hook\nclass H(Hook):\n    pass\n",
    )
    assert gate.main([]) == 0


def test_missing_explicit_target_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main([str(repo / "nope.py")]) == 1
    assert "not found" in capsys.readouterr().err
