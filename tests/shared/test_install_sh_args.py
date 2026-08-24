"""`scripts/install.sh` argument contract.

The script validates `--role` (a comma-separated capability set, no default)
before ANY side effect — argument parsing and role validation are the first
statements, and even a valid role hits the install-dir guard before the first
mutation. So these subprocess runs are safe from any cwd: both paths exit 2
with the usage text and touch nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_install_sh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, test-controlled args
        ["bash", "scripts/install.sh", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_install_sh_no_args_fails_with_usage() -> None:
    """No arguments: exit non-zero with the role usage text, before any mutation."""
    proc = _run_install_sh()
    assert proc.returncode != 0
    assert "--role" in proc.stderr, f"expected --role usage on stderr, got: {proc.stderr!r}"


def test_install_sh_bogus_role_fails() -> None:
    """An unknown capability token: exit non-zero with the role usage text."""
    proc = _run_install_sh("--role", "bogus")
    assert proc.returncode != 0
    assert "--role" in proc.stderr, f"expected --role usage on stderr, got: {proc.stderr!r}"


def test_install_sh_mirror_requires_argument() -> None:
    """`--mirror` with no value fails in arg parsing (before any guard)."""
    proc = _run_install_sh("--role", "gateway", "--mirror")
    assert proc.returncode != 0
    assert "--mirror requires an argument" in proc.stderr, (
        f"expected --mirror arg error on stderr, got: {proc.stderr!r}"
    )


def test_install_sh_mirror_flag_is_recognized() -> None:
    """`--mirror cn` is a known flag: parsing accepts it, so the run reaches the
    install-dir guard (cwd is the repo, not $AVA_HOME/source) rather than dying on
    'unknown flag'. This proves the flag wiring without running apply_mirror, which
    only fires after the guard."""
    proc = _run_install_sh("--role", "gateway,agent-runner", "--mirror", "cn")
    assert proc.returncode != 0
    assert "unknown flag" not in proc.stderr, f"--mirror cn rejected as unknown: {proc.stderr!r}"
    assert "must run from" in proc.stderr, (
        f"expected the install-dir guard (flag accepted), got: {proc.stderr!r}"
    )


def test_install_sh_worktree_excludes_role() -> None:
    """`--worktree` fixes the capability set (gateway,agent-runner); combining it
    with --role fails in cross-validation, before any side effect."""
    proc = _run_install_sh("--worktree", "--role", "gateway")
    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stderr, f"expected exclusivity error, got: {proc.stderr!r}"


def test_install_sh_worktree_excludes_mirror() -> None:
    proc = _run_install_sh("--worktree", "--mirror", "cn")
    assert proc.returncode != 0
    assert "--mirror" in proc.stderr, f"expected --mirror refusal, got: {proc.stderr!r}"


def test_install_sh_rejects_warm_mcp_as_unknown_flag() -> None:
    """The removed MCP warm-up switch must not be accepted by the installer."""
    proc = _run_install_sh("--worktree", "--warm-mcp")
    assert proc.returncode != 0
    assert "unknown flag" in proc.stderr, f"expected unknown-flag error, got: {proc.stderr!r}"


def test_install_sh_has_no_cluster_flag() -> None:
    """Identity is the path — the install surface exposes no cluster-name flag.
    `--cluster` must die as an unknown flag, not be quietly accepted."""
    proc = _run_install_sh("--worktree", "--cluster", "somename")
    assert proc.returncode != 0
    assert "unknown flag" in proc.stderr, f"expected unknown-flag error, got: {proc.stderr!r}"


def test_install_sh_path_requires_worktree() -> None:
    proc = _run_install_sh("--role", "gateway", "--path", "/nonexistent/wt-home")
    assert proc.returncode != 0
    assert "requires --worktree" in proc.stderr, f"expected --path gating, got: {proc.stderr!r}"


def test_install_sh_no_seed_requires_worktree() -> None:
    proc = _run_install_sh("--role", "gateway", "--no-seed")
    assert proc.returncode != 0
    assert "requires --worktree" in proc.stderr, f"expected --no-seed gating, got: {proc.stderr!r}"


def test_install_sh_path_requires_argument() -> None:
    proc = _run_install_sh("--worktree", "--path")
    assert proc.returncode != 0
    assert "--path requires an argument" in proc.stderr
