"""`scripts/install.sh --worktree` driver contract, exercised with stub binaries.

The script is copied into a scratch checkout and run with a stubbed `uv` + stub
`.venv/bin/python` that only record their argv/cwd, so the full worktree flow is
driven end-to-end (arg handling -> uv sync -> the cli.install_cluster birth call)
without touching a real venv, registry, or data plane. HOME is pointed at the
scratch dir so the default-home derivation is observable and nothing host-global
can be written.

What this pins:
- the checkout (and the default home name) derive from the script's own
  location, NEVER the cwd — the run's cwd is a decoy directory;
- the locked Python installer runs inside the checkout;
- the birth call carries --home/--role gateway,agent-runner/--worktree, --seed
  by default (dropped by --no-seed), and --path overrides the default home;
- no host-global step runs (no symlink under ~/.local/bin, no brew/apt).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _scaffold(tmp_path: Path, checkout_name: str) -> tuple[Path, Path, Path]:
    """Scratch checkout with the real install.sh + recording stubs for `uv` (on
    PATH) and `.venv/bin/python`. Returns (checkout, call_log, fake_home)."""
    checkout = tmp_path / checkout_name
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "scripts" / "install.sh", checkout / "scripts" / "install.sh")
    shutil.copy(
        _REPO_ROOT / "scripts" / "guard_editable_venv.py",
        checkout / "scripts" / "guard_editable_venv.py",
    )

    log = tmp_path / "calls.log"
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    uv = stub_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        '[ "${AVA_INSTALL_CLUSTER_SECRET+x}" = x ] && '
        f'echo "uv-install-secret-env=present" >> "{log}"\n'
        '[ "${CLUSTER_SECRET+x}" = x ] && '
        f'echo "uv-shell-secret-env=present" >> "{log}"\n'
        f'echo "uv $* cwd=$PWD" >> "{log}"\n'
    )
    uv.chmod(0o755)

    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text(
        "#!/bin/sh\n"
        '[ "${AVA_INSTALL_CLUSTER_SECRET+x}" = x ] && '
        f'echo "python-install-secret-env=present" >> "{log}"\n'
        '[ "${CLUSTER_SECRET+x}" = x ] && '
        f'echo "python-shell-secret-env=present" >> "{log}"\n'
        f'echo "python $* cwd=$PWD" >> "{log}"\n'
    )
    python.chmod(0o755)

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    return checkout, log, fake_home


def _run_worktree(
    tmp_path: Path, checkout_name: str, *args: str, env_extra: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str], Path, Path]:
    checkout, log, fake_home = _scaffold(tmp_path, checkout_name)
    decoy_cwd = tmp_path / "decoy-cwd"  # NOT the checkout — proves cwd independence
    decoy_cwd.mkdir()
    stub_bin = tmp_path / "stub-bin"
    proc = subprocess.run(  # noqa: S603 — fixed argv, test-controlled args
        ["bash", str(checkout / "scripts" / "install.sh"), "--worktree", *args],
        cwd=decoy_cwd,
        env={
            "PATH": f"{stub_bin}:/usr/bin:/bin",
            "HOME": str(fake_home),
            **(env_extra or {}),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls, checkout, fake_home


def test_worktree_flow_derives_checkout_and_home_from_script_location(tmp_path: Path) -> None:
    proc, calls, checkout, fake_home = _run_worktree(tmp_path, "myclone")
    assert proc.returncode == 0, f"stderr: {proc.stderr!r}"
    assert len(calls) == 2, f"expected exactly locked install + birth, got: {calls}"

    uv_call, birth_call = calls
    assert (
        uv_call
        == f"uv run --no-project --python 3.12 python {checkout}/cli/python_install.py --locked --inexact --mirror-env {fake_home}/.ava-myclone/mirror.env cwd={checkout}"
    )
    assert birth_call.startswith("python -m cli.install_cluster ")
    assert f"--home {fake_home}/.ava-myclone" in birth_call  # default home from checkout name
    assert "--role gateway,agent-runner" in birth_call
    assert "--worktree" in birth_call
    assert "--seed" in birth_call  # seeding is the worktree default
    assert f"cwd={checkout}" in birth_call  # birth runs inside the checkout


def test_worktree_flow_skips_host_global_steps(tmp_path: Path) -> None:
    proc, _calls, _checkout, fake_home = _run_worktree(tmp_path, "myclone")
    assert proc.returncode == 0, f"stderr: {proc.stderr!r}"
    # no ~/.local/bin/ava symlink, no ~/.ava mutation — worktree mode is
    # checkout-scoped (HOME points at the scratch dir, so any violation shows up)
    assert not (fake_home / ".local").exists()
    assert not (fake_home / ".ava").exists()


def test_worktree_no_seed_drops_seed_flag(tmp_path: Path) -> None:
    proc, calls, _checkout, _fake_home = _run_worktree(tmp_path, "myclone", "--no-seed")
    assert proc.returncode == 0, f"stderr: {proc.stderr!r}"
    birth_call = calls[-1]
    assert "--seed" not in birth_call


def test_worktree_path_overrides_default_home(tmp_path: Path) -> None:
    custom = tmp_path / "custom-home"
    proc, calls, _checkout, fake_home = _run_worktree(tmp_path, "myclone", "--path", str(custom))
    assert proc.returncode == 0, f"stderr: {proc.stderr!r}"
    birth_call = calls[-1]
    assert f"--home {custom}" in birth_call
    assert f"{fake_home}/.ava-myclone" not in birth_call


def test_worktree_install_secret_reaches_only_birth_child_and_never_argv(tmp_path: Path) -> None:
    secret = "sentinel-install-secret"  # noqa: S105 — test fixture
    proc, calls, _checkout, _fake_home = _run_worktree(
        tmp_path,
        "myclone",
        env_extra={
            "AVA_INSTALL_CLUSTER_SECRET": secret,
            "CLUSTER_SECRET": "ambient-implementation-detail",
        },
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr!r}"
    assert "uv-install-secret-env=present" not in calls
    assert "uv-shell-secret-env=present" not in calls
    assert "python-install-secret-env=present" in calls
    assert "python-shell-secret-env=present" not in calls
    birth_call = calls[-1]
    assert secret not in birth_call
    assert "--cluster-secret" not in birth_call


def test_worktree_requires_uv_on_path(tmp_path: Path) -> None:
    """Without uv the worktree flow dies with an actionable message (host-global
    toolchain install is deliberately NOT run in worktree mode)."""
    checkout, log, fake_home = _scaffold(tmp_path, "myclone")
    (tmp_path / "stub-bin" / "uv").unlink()
    proc = subprocess.run(  # noqa: S603 — fixed argv, test-controlled args
        ["bash", str(checkout / "scripts" / "install.sh"), "--worktree"],
        cwd=tmp_path,
        env={"PATH": f"{tmp_path / 'stub-bin'}:/usr/bin:/bin", "HOME": str(fake_home)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode != 0
    assert "uv" in proc.stderr
    assert not log.exists(), "nothing may run when the uv prerequisite is missing"


def test_worktree_guard_refuses_symlinked_venv_before_uv_sync(tmp_path: Path) -> None:
    """The installer must fail before its first venv-writing child can run."""
    checkout, log, fake_home = _scaffold(tmp_path, "myclone")
    shutil.rmtree(checkout / ".venv")
    external = tmp_path / "external-venv"
    external.mkdir()
    (checkout / ".venv").symlink_to(external, target_is_directory=True)
    proc = subprocess.run(  # noqa: S603 — fixed repository-owned script
        ["bash", str(checkout / "scripts" / "install.sh"), "--worktree"],
        cwd=tmp_path,
        env={"PATH": f"{tmp_path / 'stub-bin'}:/usr/bin:/bin", "HOME": str(fake_home)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 1
    assert str(external) in proc.stderr
    assert not log.exists(), "uv and cluster birth must not run after a guard violation"


def test_install_sh_syntax_ok() -> None:
    """`bash -n` gate — a syntax error in install.sh must fail tests, not a fresh host."""
    proc = subprocess.run(  # noqa: S603 — fixed argv
        ["bash", "-n", str(_REPO_ROOT / "scripts" / "install.sh")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
