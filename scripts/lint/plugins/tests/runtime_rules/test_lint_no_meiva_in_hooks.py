from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint.plugins import no_meiva_in_hooks as gate


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    for scan_dir in gate.SCAN_DIRS:
        (tmp_path / scan_dir).mkdir()
    return tmp_path


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "statement",
    [
        "import meiva",
        "import meiva.sdk_surface.agent_identity as ident",
        "from meiva import skills",
        "from meiva.sdk_surface import install",
    ],
)
def test_meiva_import_in_a_hook_subclass_module_is_reported(
    repo: Path, capsys: pytest.CaptureFixture[str], statement: str
) -> None:
    _write(repo, "meiva_builtins/plugins/p/hook.py", f"{statement}\nclass H(Hook):\n    pass\n")
    assert gate.main([]) == 1
    assert "meiva_builtins/plugins/p/hook.py:1:" in capsys.readouterr().out


def test_deferred_and_type_checking_imports_are_reported(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        "meiva_builtins/plugins/p/agent_runtime.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import meiva\n"
        "def f():\n"
        "    from meiva.skills import identifier\n",
    )
    assert gate.main([]) == 1
    out = capsys.readouterr().out
    assert "agent_runtime.py:3:" in out
    assert "agent_runtime.py:5:" in out


def test_hooks_package_module_is_a_hook_module_without_a_subclass(repo: Path) -> None:
    _write(repo, "agent/hooks/helper.py", "import meiva\n")
    assert gate.main([]) == 1


def test_dotted_hook_base_is_recognised(repo: Path) -> None:
    _write(repo, "demos/d/gate.py", "import meiva\nclass G(hooks.Hook):\n    pass\n")
    assert gate.main([]) == 1


def test_non_hook_module_and_tests_may_import_meiva(repo: Path) -> None:
    _write(repo, "meiva_builtins/plugins/p/plugin.py", "import meiva\n")
    _write(repo, "agent/graph/capabilities.py", "def f():\n    import meiva\n")
    _write(repo, "agent/hooks/tests/test_x.py", "import meiva\nclass H(Hook):\n    pass\n")
    assert gate.main([]) == 0


def test_hook_module_without_meiva_import_passes(repo: Path) -> None:
    _write(
        repo,
        "meiva_builtins/plugins/p/agent_runtime.py",
        "from . import plugin\nfrom agent.hooks import Hook\nclass H(Hook):\n    pass\n",
    )
    assert gate.main([]) == 0


def test_missing_explicit_target_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main([str(repo / "nope.py")]) == 1
    assert "not found" in capsys.readouterr().err
