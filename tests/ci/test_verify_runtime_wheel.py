"""The runtime wheel carries the application, never the tests that live beside it.

Tests sit in `<pkg>/**/tests/` inside the packages the wheel packs, so the wheel
build must exclude them and the closure proof must fail if one ever ships.
"""

from __future__ import annotations

import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.ci import verify_runtime_wheel

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _wheel(path: Path, extra: tuple[str, ...] = ()) -> Path:
    members = (
        *verify_runtime_wheel.REQUIRED,
        *(f"{package}/__init__.py" for package in verify_runtime_wheel.PACKAGES),
        *extra,
    )
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, "")
    return path


def test_a_complete_wheel_without_tests_verifies(tmp_path: Path) -> None:
    verify_runtime_wheel.verify_members(_wheel(tmp_path / "ok.whl"))


@pytest.mark.parametrize(
    "member",
    [
        "base/tests/test_x.py",
        "base/packages/tests/test_x.py",
        "ava_builtins/skills/gmail/scripts/tests/test_gmail.py",
        "services/pitr/stores/cos/tests/data/sample.json",
    ],
)
def test_a_wheel_shipping_a_test_directory_is_rejected(tmp_path: Path, member: str) -> None:
    wheel = _wheel(tmp_path / "leaky.whl", (member,))
    with pytest.raises(ValueError, match="runtime wheel ships test files"):
        verify_runtime_wheel.verify_members(wheel)


def test_a_module_named_test_outside_a_tests_directory_is_application_code(
    tmp_path: Path,
) -> None:
    """`base/db/test_db_guard.py` is production code and must ship."""
    verify_runtime_wheel.verify_members(_wheel(tmp_path / "ok.whl", ("base/db/test_db_guard.py",)))


def test_the_wheel_build_excludes_every_tests_directory() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "**/tests/**" in wheel["exclude"]
