"""Process-level checks for the dependency-free worktree venv preflight."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARD = _REPO_ROOT / "scripts" / "guard_editable_venv.py"


def _run(checkout: Path, *, virtual_env: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the real script under a deliberate environment, never the host's venv."""
    env = {"PATH": os.environ["PATH"]}
    if virtual_env is not None:
        env["VIRTUAL_ENV"] = str(virtual_env)
    return subprocess.run(  # noqa: S603 — sys.executable + repository-owned script
        [sys.executable, str(_GUARD), str(checkout)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_pth(checkout: Path, target: Path) -> Path:
    path = checkout / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    path.parent.mkdir(parents=True)
    path.write_text(str(target))
    return path


def test_symlinked_worktree_venv_is_refused_with_its_real_target(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    external = tmp_path / "external-venv"
    external.mkdir()
    (checkout / ".venv").symlink_to(external, target_is_directory=True)

    result = _run(checkout)

    assert result.returncode == 1
    assert str(external) in result.stderr
    assert "symlink" in result.stderr


def test_external_virtual_env_is_refused_with_unset_hint(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".venv").mkdir(parents=True)
    external = tmp_path / "external-venv"
    external.mkdir()

    result = _run(checkout, virtual_env=external)

    assert result.returncode == 1
    assert str(external) in result.stderr
    assert "env -u VIRTUAL_ENV" in result.stderr


def test_real_checkout_venv_without_virtual_env_is_safe(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".venv").mkdir(parents=True)

    result = _run(checkout)

    assert result.returncode == 0
    assert result.stderr == ""


def test_repeated_checkout_editable_pointer_entries_are_safe(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    pth = _write_pth(checkout, checkout)
    pth.write_text(f"{checkout}\n{checkout}")

    result = _run(checkout)

    assert result.returncode == 0
    assert result.stderr == ""


def test_checkout_editable_pointer_to_another_source_is_refused(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    external = tmp_path / "other-checkout"
    _write_pth(checkout, external)

    result = _run(checkout)

    assert result.returncode == 1
    assert str(external) in result.stderr
    assert "_editable_impl_ava.pth" in result.stderr
