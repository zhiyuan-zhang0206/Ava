"""Public entry contract for the repository tailnet literal gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.content_lint import lint_no_tailnet as gate


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Use a real Git index for default scans without changing the checkout."""
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    return tmp_path


def _write(repo: Path, rel: str, content: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _cgnat_ip(second_octet: int, tail: str = "0.1") -> str:
    """Build runtime fixtures without embedding banned host literals."""
    return f"100.{second_octet}.{tail}"


@pytest.mark.parametrize(
    ("second_octet", "tail"), [(64, "0.2"), (78, "137.46"), (101, "102.103"), (127, "255.255")]
)
def test_in_range_host_is_reported(
    repo: Path, capsys: pytest.CaptureFixture[str], second_octet: int, tail: str
) -> None:
    literal = _cgnat_ip(second_octet, tail)
    _write(repo, "tests/fixture.py", f"clean\n  url = 'http://{literal}:8000/'  \n")
    assert gate.main(["tests/fixture.py"], repo_root=repo) == 1
    output = capsys.readouterr()
    assert output.out == f"tests/fixture.py:2: {literal} | url = 'http://{literal}:8000/'\n"
    assert "Tailnet IP literal found" in output.err


@pytest.mark.parametrize(
    "content",
    [
        "No hosts here.\n",
        "127.0.0.1 192.168.1.10 10.1.2.3 172.16.0.9\n",
        "100.128.0.1 100.255.255.255 100.63.255.255\n",
        "trusted cidrs: 100.64.0.0/10\n",
        (_cgnat_ip(64) + "\x00more").encode(),
        _cgnat_ip(64).encode() + b"\xff\xfe",
    ],
)
def test_allowed_text_and_binary_files_pass(
    repo: Path, capsys: pytest.CaptureFixture[str], content: str | bytes
) -> None:
    _write(repo, "assets/data.txt", content)
    assert gate.main(["assets/data.txt"], repo_root=repo) == 0
    assert capsys.readouterr() == ("", "")


def test_url_path_is_not_a_cidr_mask(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    literal = _cgnat_ip(64)
    _write(repo, "docs/guide.md", f"url = 'http://{literal}/status'\n")
    assert gate.main(["docs/guide.md"], repo_root=repo) == 1
    assert (
        capsys.readouterr().out == f"docs/guide.md:1: {literal} | url = 'http://{literal}/status'\n"
    )


def test_opt_out_applies_only_to_its_own_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    boundary = _cgnat_ip(101, "102.103")
    other = _cgnat_ip(64, "0.9")
    _write(
        repo, "tests/fixture.py", f"a = '{boundary}'  # tailnet-ip-ok: boundary\nb = '{other}'\n"
    )
    assert gate.main(["tests/fixture.py"], repo_root=repo) == 1
    assert capsys.readouterr().out == f"tests/fixture.py:2: {other} | b = '{other}'\n"
    _write(repo, "tests/fixture.py", f"a = '{boundary}'  # tailnet-ip-ok: boundary\n")
    assert gate.main(["tests/fixture.py"], repo_root=repo) == 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("docs/decisions/runtime/history.md", 0),
        ("docs/decisions-extra/history.md", 1),
        ("docs/guide.md", 1),
    ],
)
def test_frozen_exemption_is_limited_to_decisions(repo: Path, rel: str, expected: int) -> None:
    _write(repo, rel, f"gateway {_cgnat_ip(103, '96.72')}\n")
    assert gate.main([rel], repo_root=repo) == expected


def test_default_scan_uses_git_index_and_line_order(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _cgnat_ip(64, "0.2")
    second = _cgnat_ip(127, "0.3")
    _write(repo, "docs/z.md", f"clean\n  host = '{first}'  \n{second}\n")
    _write(repo, "docs/a.md", f"host = '{second}'\n")
    _write(repo, "untracked.txt", f"{first}\n")
    subprocess.run(["git", "add", "docs/z.md", "docs/a.md"], cwd=repo, check=True)
    assert gate.main([], repo_root=repo) == 1
    assert capsys.readouterr().out == (
        f"docs/a.md:1: {second} | host = '{second}'\n"
        f"docs/z.md:2: {first} | host = '{first}'\n"
        f"docs/z.md:3: {second} | {second}\n"
    )
    _write(repo, "docs/a.md", "clean\n")
    _write(repo, "docs/z.md", "clean\n")
    assert gate.main([], repo_root=repo) == 0
    assert capsys.readouterr() == ("", "")


def test_explicit_targets_are_untracked_sorted_and_deduplicated(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _cgnat_ip(64)
    second = _cgnat_ip(127)
    _write(repo, "docs/z.md", f"{first}\n")
    _write(repo, "docs/a.md", f"{second}\n")
    assert gate.main(["docs/z.md", "docs"], repo_root=repo) == 1
    assert (
        capsys.readouterr().out
        == f"docs/a.md:1: {second} | {second}\ndocs/z.md:1: {first} | {first}\n"
    )


def test_missing_target_fails_before_scanning_existing_targets(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "bad.txt", f"{_cgnat_ip(64)}\n")
    for args in (["typo.txt"], [str(repo / "typo.txt")], ["bad.txt", "typo.txt"]):
        assert gate.main(args, repo_root=repo) == 1
        output = capsys.readouterr()
        assert output.out == ""
        assert "target path(s) not found" in output.err
        assert "typo.txt" in output.err


def test_outside_directory_is_scanned(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outside = repo.parent / f"{repo.name}-outside"
    outside.mkdir()
    _write(outside, "data.txt", "No hosts.\n")
    assert gate.main([str(outside)], repo_root=repo) == 0
    assert capsys.readouterr() == ("", "")
    literal = _cgnat_ip(64)
    _write(outside, "data.txt", f"{literal}\n")
    assert gate.main([str(outside)], repo_root=repo) == 1
    assert capsys.readouterr().out == f"{outside}/data.txt:1: {literal} | {literal}\n"


def test_only_scope_and_tooling_widening(repo: Path) -> None:
    _write(repo, "docs/bad.md", f"{_cgnat_ip(64)}\n")
    _write(repo, "docs/clean.md", "clean\n")
    _write(repo, "scripts/content_lint/tool.py", "")
    subprocess.run(["git", "add", "docs", "scripts"], cwd=repo, check=True)
    assert gate.main(["--only", "docs/bad.md"], repo_root=repo) == 1
    assert gate.main(["--only", "docs/clean.md"], repo_root=repo) == 0
    assert gate.main(["--only"], repo_root=repo) == 0
    assert (
        gate.main(["--only", "docs/clean.md", "scripts/content_lint/tool.py"], repo_root=repo) == 1
    )


def test_unknown_only_target_is_rejected_before_explicit_scan(repo: Path) -> None:
    _write(repo, "docs/clean.md", "clean\n")
    with pytest.raises(SystemExit, match=r"not found: typo\.txt"):
        gate.main(["docs/clean.md", "--only", "typo.txt"], repo_root=repo)
