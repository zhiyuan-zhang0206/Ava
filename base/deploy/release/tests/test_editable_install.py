"""Regression guards for the read-only editable-install inspection shared by exec and health."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from base.deploy.release import editable_install


def _write_pth(source_root: Path, target: Path) -> Path:
    pth = source_root / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(target))
    return pth


def _write_direct_url(source_root: Path, url: str, *, editable: bool = True) -> Path:
    """A direct_url.json matching uv's editable-install record shape."""
    du = (
        source_root
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "ava-0.1.5.dist-info"
        / "direct_url.json"
    )
    du.parent.mkdir(parents=True)
    du.write_text(json.dumps({"url": url, "dir_info": {"editable": editable}}))
    return du


def test_worktree_below_allowlisted_dev_clone_is_a_violation(tmp_path: Path) -> None:
    """Allowlisting a clone root must not implicitly allow its disposable worktrees."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    worktree = dev_clone / ".worktrees" / "feature"
    worktree.mkdir(parents=True)
    pth = _write_pth(source_root, worktree)
    du = _write_direct_url(source_root, worktree.as_uri())

    violations = editable_install.editable_install_violations(
        source_root, allowed_roots=(dev_clone,)
    )

    assert len(violations) == 2
    assert str(pth) in violations[0] and str(du) in violations[1]
    assert pth.read_text() == str(worktree)  # inspection never rewrites


def test_a_legal_pointer_is_clean_without_a_trailing_newline(tmp_path: Path) -> None:
    """Pointer legality is semantic rather than a trailing-newline byte form."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())
    pth.write_text(str(source_root))

    assert editable_install.editable_install_violations(source_root) == ()


def test_crlf_semantic_pth_is_clean(tmp_path: Path) -> None:
    """Universal-newline reading accepts a CRLF pointer."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())
    pth.write_bytes(f"{source_root}\r\n".encode())

    assert editable_install.editable_install_violations(source_root) == ()


def test_empty_pth_is_reported(tmp_path: Path) -> None:
    """An empty pointer cannot be interpreted as a legal editable target."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())
    pth.write_text("")

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1
    assert "names" in violations[0]
    assert pth.read_text() == ""  # inspection never rewrites


def test_missing_direct_url_beside_pth_is_reported(tmp_path: Path) -> None:
    """A removed direct-URL record is a half-uninstall violation."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    direct_url.unlink()

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1
    assert str(pth) in violations[0]
    assert "metadata missing" in violations[0]
    assert not direct_url.exists()  # the operator's `uv sync` recreates it, not inspection


def test_missing_dist_info_beside_pth_is_reported(tmp_path: Path) -> None:
    """A pointer with no dist-info directory at all is a half-uninstall violation."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1
    assert str(pth) in violations[0]
    assert "metadata missing" in violations[0]


def test_half_uninstall_pointer_missing_beside_metadata_is_reported(tmp_path: Path) -> None:
    """Existing editable metadata makes a missing pointer a violation."""
    source_root = tmp_path / "prod" / "source"
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    pth = direct_url.parent.parent / editable_install.EDITABLE_PTH_NAME

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1
    assert str(pth) in violations[0]
    assert "metadata present but pointer missing" in violations[0]
    assert not pth.exists()


def test_both_missing_records_are_clean(tmp_path: Path) -> None:
    """A venv with no editable records has nothing to assert."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)

    assert editable_install.editable_install_violations(source_root) == ()


def test_non_editable_direct_url_is_a_violation(tmp_path: Path) -> None:
    """A record denying editability contradicts the pointer beside it."""
    source_root = tmp_path / "prod" / "source"
    _write_pth(source_root, source_root)
    du = _write_direct_url(source_root, source_root.as_uri(), editable=False)

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1 and str(du) in violations[0]


def test_exact_allowlisted_dev_clone_direct_url_is_clean(tmp_path: Path) -> None:
    """The exact-root allowlist applies to the direct_url record too."""
    source_root = tmp_path / "prod" / "source"
    dev_clone = tmp_path / "Ava"
    dev_clone.mkdir(parents=True)
    _write_pth(source_root, dev_clone)
    _write_direct_url(source_root, dev_clone.as_uri())

    assert (
        editable_install.editable_install_violations(source_root, allowed_roots=(dev_clone,)) == ()
    )


def test_editable_console_script_violations_follow_the_posix_venv_layout(tmp_path: Path) -> None:
    """The POSIX launcher is judged beside the POSIX venv interpreter."""

    source_root = tmp_path / "source"
    interpreter = source_root / ".venv" / "bin" / "python"
    console_script = interpreter.with_name("ava")
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    console_script.touch()

    assert editable_install.editable_console_script_violations(source_root) == ()

    console_script.unlink()

    assert editable_install.editable_console_script_violations(source_root) == (
        f"{console_script} editable console script missing",
    )


def test_editable_install_violations_healthy_is_empty(tmp_path: Path) -> None:
    """Legal records (prod root + editable URL) report no violations."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())

    assert editable_install.editable_install_violations(source_root) == ()


def test_editable_install_violations_accepts_repeated_checkout_pth_entries(
    tmp_path: Path,
) -> None:
    """Repeated allowed entries are uv's normal one-line-per-wheel layout."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    pth = _write_pth(source_root, source_root)
    _write_direct_url(source_root, source_root.as_uri())
    pth.write_text(f"{source_root}\n{source_root}")

    assert editable_install.editable_install_violations(source_root) == ()


def test_editable_install_violations_allowlisted_clone_is_empty(tmp_path: Path) -> None:
    """Exact-root allowlisting applies to the read-only report too."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    dev_clone = tmp_path / "Ava"
    _write_pth(source_root, dev_clone)
    _write_direct_url(source_root, dev_clone.as_uri())

    assert (
        editable_install.editable_install_violations(source_root, allowed_roots=(dev_clone,)) == ()
    )


def test_editable_install_violations_reports_poisoned_records(tmp_path: Path) -> None:
    """Both records are reported, each naming the disposable source."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    worktree = tmp_path / "deleted-worktree"
    pth = _write_pth(source_root, worktree)
    record = _write_direct_url(source_root, worktree.as_uri())

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 2
    assert str(pth) in violations[0] and str(worktree) in violations[0]
    assert str(record) in violations[1] and str(worktree) in violations[1]


def test_editable_install_violations_unparsable_record_is_reported(
    tmp_path: Path,
) -> None:
    """A record that cannot be parsed cannot be verified — reported."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    _write_pth(source_root, source_root)
    record = _write_direct_url(source_root, source_root.as_uri())
    record.write_text("{not json")

    violations = editable_install.editable_install_violations(source_root)

    assert len(violations) == 1 and str(record) in violations[0]


_GUARD_PROBE = Path(__file__).with_name("_guard_probe_child.py")


@pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is required for the native editable test"
)
def test_uv_native_editable_records_are_legal_and_exec_guard_accepts_them(tmp_path: Path) -> None:
    """A real uv install keeps its per-wheel pointer byte-identical through both guards."""

    checkout = Path(__file__).parents[4]
    uv_built_venv_root = tmp_path / "uv-built-venv"
    venv.create(uv_built_venv_root / ".venv", with_pip=False, symlinks=True)
    interpreter = uv_built_venv_root / ".venv" / "bin" / "python"
    install = subprocess.run(  # noqa: S603 — test-owned venv and checkout path
        [
            "env",
            "-u",
            "VIRTUAL_ENV",
            "uv",
            "pip",
            "install",
            "-e",
            str(checkout),
            "--python",
            str(interpreter),
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr

    pth_paths = editable_install.editable_ava_pth_paths(uv_built_venv_root)
    assert pth_paths
    before = {path: path.read_bytes() for path in pth_paths}
    assert (
        editable_install.editable_install_violations(
            uv_built_venv_root,
            allowed_roots=(checkout,),
        )
        == ()
    )
    assert (
        editable_install.editable_install_violations(
            uv_built_venv_root,
            allowed_roots=(checkout,),
        )
        == ()
    )
    assert {path: path.read_bytes() for path in pth_paths} == before

    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(checkout)
    child = subprocess.run(  # noqa: S603 — current test interpreter and test-owned child script
        [
            sys.executable,
            str(_GUARD_PROBE),
            str(uv_built_venv_root),
            str(checkout),
        ],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert child.returncode == 0, child.stderr


def test_current_interpreter_source_root_reads_a_posix_venv_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exec guard anchors itself to the interpreter's checkout, not cwd."""
    source_root = tmp_path / "checkout"
    interpreter = source_root / ".venv" / "bin" / "python"
    monkeypatch.setattr(editable_install.sys, "executable", str(interpreter))

    assert editable_install.current_interpreter_source_root() == source_root


def test_current_interpreter_source_root_keeps_symlinked_venv_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolving the base interpreter must not discard its enclosing virtualenv."""
    source_root = tmp_path / "checkout"
    base_python = tmp_path / "base" / "python"
    base_python.parent.mkdir(parents=True)
    base_python.write_text("")
    interpreter = source_root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(base_python)
    monkeypatch.setattr(editable_install.sys, "executable", str(interpreter))

    assert editable_install.current_interpreter_source_root() == source_root
