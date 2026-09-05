"""Regional mirror transport must never become the shared frozen CI lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_python_lock as gate


def _lock(path: Path, *, index: str, wheel: str, sdist: str) -> Path:
    path.write_text(
        f"""version = 1
[[package]]
name = "example"
version = "1.0"
source = {{ registry = "{index}" }}
sdist = {{ url = "{sdist}", hash = "sha256:abc" }}
wheels = [{{ url = "{wheel}", hash = "sha256:def" }}]
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("changed", ["index", "wheel", "sdist"])
def test_rejects_mirror_index_or_artifact(tmp_path: Path, changed: str) -> None:
    values = {
        "index": "https://pypi.org/simple",
        "wheel": "https://files.pythonhosted.org/packages/example.whl",
        "sdist": "https://files.pythonhosted.org/packages/example.tar.gz",
    }
    values[changed] = "https://mirror.example/simple-or-package"
    path = _lock(tmp_path / "uv.lock", **values)
    assert len(gate.violations(path)) == 1


def test_canonical_lock_and_explicit_local_mirror_are_independent(tmp_path: Path) -> None:
    path = _lock(
        tmp_path / "uv.lock",
        index="https://pypi.org/simple",
        wheel="https://files.pythonhosted.org/packages/example.whl",
        sdist="https://files.pythonhosted.org/packages/example.tar.gz",
    )
    (tmp_path / "mirror.env").write_text(
        "UV_DEFAULT_INDEX=https://mirror.example/simple\n", encoding="utf-8"
    )
    assert gate.violations(path) == []


def test_repository_lock_is_canonical() -> None:
    """Run against the real lock so a host-generated replacement fails this guard."""
    assert gate.violations(gate._REPO_ROOT / "uv.lock") == []
