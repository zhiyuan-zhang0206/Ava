"""Explicit editable-install write windows preserve protected records."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from base.deploy.release import editable_install
from base.deploy.release.tests.test_editable_install import _write_direct_url, _write_pth


def test_editable_pth_write_window_allows_atomic_replacement_and_restores_modes(
    tmp_path: Path,
) -> None:
    """Only the sanctioned window opens a protected uv replacement boundary."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    site_packages = pth.parent
    site_packages.chmod(0o555)
    pth.chmod(0o444)

    with pytest.raises(PermissionError):
        pth.unlink()

    with editable_install.editable_pth_write_window(source_root):
        assert stat.S_IMODE(site_packages.stat().st_mode) == 0o755
        assert stat.S_IMODE(pth.stat().st_mode) == 0o644
        pth.unlink()
        replacement = pth.with_name(f".{pth.name}.tmp")
        replacement.write_text("replacement")
        replacement.replace(pth)

    assert pth.read_text() == "replacement"
    assert stat.S_IMODE(site_packages.stat().st_mode) == 0o555
    assert stat.S_IMODE(pth.stat().st_mode) == 0o444


def test_editable_pth_write_window_opens_hardened_dist_info_directory(
    tmp_path: Path,
) -> None:
    """A hardened 0o555 ava dist-info dir must be writable inside the window.

    Regression for the 2026-09-03 rollout: a converged host carried a read-only
    ``ava-*.dist-info`` directory, and uv's reinstall uninstall removes the
    files *inside* that directory (INSTALLER, RECORD, ...) before it can
    rewrite the distribution — a write operation that needs owner write on the
    dist-info directory itself, not only on its parent site-packages. The
    window must open it and restore the exact original mode afterwards.
    """

    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, source_root)
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    dist_info = direct_url.parent
    site_packages = pth.parent
    installer = dist_info / "INSTALLER"
    installer.write_text("uv\n")
    site_packages.chmod(0o555)
    dist_info.chmod(0o555)

    with pytest.raises(PermissionError):
        installer.unlink()

    with editable_install.editable_pth_write_window(source_root):
        assert stat.S_IMODE(site_packages.stat().st_mode) == 0o755
        assert stat.S_IMODE(dist_info.stat().st_mode) == 0o755
        installer.unlink()
        installer.write_text("uv\n")

    assert installer.read_text() == "uv\n"
    assert stat.S_IMODE(site_packages.stat().st_mode) == 0o555
    assert stat.S_IMODE(dist_info.stat().st_mode) == 0o555


def test_protected_paths_survive_a_sync_that_deleted_the_records(tmp_path: Path) -> None:
    """A partial sync cannot hide its protected directories by deleting records."""
    source_root = tmp_path / "prod" / "source"
    site_packages = source_root / ".venv" / "lib" / "python3.12" / "site-packages"
    bin_dir = source_root / ".venv" / "bin"
    for path in (site_packages, bin_dir):
        path.mkdir(parents=True)
        path.chmod(0o555)
    assert editable_install.protected_editable_paths(source_root) == (site_packages, bin_dir)


def test_write_window_skips_path_that_disappears_before_entry(tmp_path: Path) -> None:
    """A venv recreation race cannot abort the sync before it starts."""
    vanished_path = tmp_path / "vanished.pth"
    vanished_path.write_text("temporary")
    vanished_path.unlink()

    with editable_install._write_window((vanished_path,)):
        pass


def test_repair_editable_install_opens_protected_site_packages_directory(
    tmp_path: Path,
) -> None:
    """Converge repair remains able to fix records after directory hardening."""
    source_root = tmp_path / "prod" / "source"
    pth = _write_pth(source_root, tmp_path / "deleted-worktree")
    direct_url = _write_direct_url(source_root, (tmp_path / "deleted-worktree").as_uri())
    site_packages = pth.parent
    site_packages.chmod(0o555)

    editable_install.repair_editable_install(source_root)

    assert pth.read_text() == str(source_root)
    assert json.loads(direct_url.read_text())["url"] == source_root.as_uri()
    assert stat.S_IMODE(site_packages.stat().st_mode) == 0o555


def test_repair_half_uninstall_opens_protected_site_packages_directory(
    tmp_path: Path,
) -> None:
    """The write window can recreate a missing pointer after directory hardening."""
    source_root = tmp_path / "prod" / "source"
    direct_url = _write_direct_url(source_root, source_root.as_uri())
    pth = direct_url.parent.parent / editable_install.EDITABLE_PTH_NAME
    site_packages = pth.parent
    site_packages.chmod(0o555)

    editable_install.repair_editable_install(source_root)

    assert pth.read_text() == str(source_root)
    assert stat.S_IMODE(site_packages.stat().st_mode) == 0o555
