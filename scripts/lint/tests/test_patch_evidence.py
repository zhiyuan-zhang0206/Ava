"""Strict execution diagnostics fail explicitly while the existing patch gate retains its contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint import patch_targets as lint
from scripts.structure.tests.patch_repo import make_repo, write


@pytest.mark.parametrize("report", [False, True])
def test_strict_execution_gap_is_a_failed_diagnostic_even_for_a_boundary_patch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, report: bool
) -> None:
    root = make_repo(tmp_path)
    write(
        root,
        "base/net/tests/test_probe.py",
        (
            "import sys, subprocess\nsubprocess.run([sys.executable, '-c', builder()])\n"
            "def test_x(monkeypatch):\n    monkeypatch.setattr('time.sleep', None)\n"
        ),
    )
    argv = ["--strict-evidence", *(["--report"] if report else [])]
    assert lint.main(argv, repo_root=root) == 1
    captured = capsys.readouterr()
    assert "base/net/tests/test_probe.py:2: incomplete execution evidence" in captured.out
    assert "strict execution-evidence diagnostics" in captured.err


def test_normal_patch_check_discloses_incomplete_legacy_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    write(
        root,
        "base/net/tests/test_probe.py",
        (
            "import sys, subprocess\nsubprocess.run([sys.executable, '-c', builder()])\n"
            "def test_x(monkeypatch):\n    monkeypatch.setattr('time.sleep', None)\n"
        ),
    )
    assert lint.main([], repo_root=root) == 0
    captured = capsys.readouterr()
    assert "Legacy patch-authority check: execution evidence is incomplete" in captured.err
    assert "base/net/tests/test_probe.py:2:" in captured.err


def test_complete_diagnostic_does_not_replace_the_existing_private_policy(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    write(
        root,
        "tests/test_probe.py",
        (
            "from base.net import retry\nretry.backoff()\n"
            "def test_x(monkeypatch):\n    monkeypatch.setattr('base.net.retry._sleep', None)\n"
        ),
    )
    assert lint.main([], repo_root=root) == 0
    assert lint.main(["--strict-evidence"], repo_root=root) == 0


def test_complete_owned_subject_and_public_boundary_pass_strict_diagnostics(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    write(
        root,
        "base/net/tests/test_probe.py",
        (
            "from base.net import retry\nretry.backoff()\n"
            "def test_x(monkeypatch):\n    monkeypatch.setattr('base.net.retry._sleep', None)\n"
            "    monkeypatch.setattr('time.sleep', None)\n"
        ),
    )
    assert lint.main(["--strict-evidence"], repo_root=root) == 0


def test_strict_completeness_cannot_certify_an_unreadable_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    path = write(root, "base/net/tests/test_probe.py", "")
    path.write_bytes(b"\xff")
    assert lint.main([], repo_root=root) == 0
    capsys.readouterr()
    assert lint.main(["--strict-evidence"], repo_root=root) == 1
    assert (
        "base/net/tests/test_probe.py:1: cannot read execution evidence" in capsys.readouterr().out
    )
