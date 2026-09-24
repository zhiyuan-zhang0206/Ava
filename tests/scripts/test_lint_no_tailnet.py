"""scripts/lint_no_tailnet.py: the repo-wide tailnet IP literal gate.

Rules (2026-08-03/04 Gateway-URL ruling + 2026-08-20 public-repo contribution
ruling): the repo must not carry a deployment's private overlay addresses as
literals; the cluster's user-visible URL derives from AVA_GATEWAY_URL /
reachable_host(). The CIDR range notation (100.64.0.0/10) is the neutral way
to NAME the range and stays allowed; decisions/ is frozen historical
narrative; a line that genuinely exercises the range boundary opts out with an
inline `# tailnet-ip-ok:` marker. These tests pin the detection, the range
boundaries, and the exemption paths. In-range fixtures are built at runtime
(see `_cgnat_ip`) because the gate's own repo must stay literal-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_no_tailnet as gate


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the gate at a scratch repo root so the real checkout is untouched."""
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    return tmp_path


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _cgnat_ip(second_octet: int, tail: str = "0.1") -> str:
    """Build an IPv4 literal inside 100.64.0.0/10 without writing the literal.

    The gate scans its own repo, so a raw in-range literal in this test file
    would be a violation; concatenation keeps the fixture runtime-identical.
    """
    return f"100.{second_octet}.{tail}"


def test_clean_file_passes(repo: Path) -> None:
    _write(repo, "shared/config.py", "# gateway URL from settings\nurl = settings.gateway_url\n")
    assert gate._scan_file("shared/config.py") == []


def test_private_10x_literal_passes(repo: Path) -> None:
    _write(repo, "tests/fixture.py", 'host = "http://10.0.0.5:8000"\n')
    assert gate._scan_file("tests/fixture.py") == []


def test_loopback_and_rfc1918_literals_pass(repo: Path) -> None:
    _write(
        repo,
        "tests/fixture.py",
        'hosts = ["127.0.0.1", "192.168.1.10", "10.1.2.3", "172.16.0.9"]\n',
    )
    assert gate._scan_file("tests/fixture.py") == []


def test_tailnet_literal_fails(repo: Path) -> None:
    literal = _cgnat_ip(103, "96.72")
    _write(repo, "ui/app/app-ui/index.html", f'placeholder="{literal}"\n')
    hits = gate._scan_file("ui/app/app-ui/index.html")
    assert len(hits) == 1
    assert hits[0][0] == 1
    assert hits[0][1] == literal


@pytest.mark.parametrize(
    ("second_octet", "tail"),
    [(64, "0.2"), (78, "137.46"), (101, "102.103"), (127, "255.255")],
)
def test_in_range_literal_fails(repo: Path, second_octet: int, tail: str) -> None:
    literal = _cgnat_ip(second_octet, tail)
    _write(repo, "tests/fixture.py", f"url = 'http://{literal}:8000/'\n")
    hits = gate._scan_file("tests/fixture.py")
    assert len(hits) == 1
    assert hits[0][1] == literal


@pytest.mark.parametrize(
    "literal",
    # Outside the /10: 100.128.0.0+ is public space, and the boundary is the
    # whole point of the mask (urls.rs pins both edges in its own tests).
    ["100.128.0.1", "100.255.255.255", "100.63.255.255"],
)
def test_out_of_range_literal_passes(repo: Path, literal: str) -> None:
    _write(repo, "tests/fixture.py", f"url = 'http://{literal}:8000/'\n")
    assert gate._scan_file("tests/fixture.py") == []


def test_cidr_range_notation_passes(repo: Path) -> None:
    """`100.64.0.0/10` names the range; it is the neutral way to document the
    policy and is not a host address."""
    _write(
        repo, "shared/netutil.py", "# VPN-overlay 100.64.0.0/10 addresses get a pinned transport\n"
    )
    assert gate._scan_file("shared/netutil.py") == []


def test_range_notation_with_numeric_mask_passes(repo: Path) -> None:
    _write(repo, "docs/runbook.md", "trusted cidrs: 100.64.0.0/10\n")
    assert gate._scan_file("docs/runbook.md") == []


def test_cidr_lookalike_url_path_still_fails(repo: Path) -> None:
    """A URL path after the host is not a CIDR mask — the literal must still
    be caught (the lookahead only accepts `/NN`)."""
    literal = _cgnat_ip(64, "0.2")
    _write(repo, "tests/fixture.py", f"url = 'http://{literal}/status'\n")
    hits = gate._scan_file("tests/fixture.py")
    assert len(hits) == 1


def test_boundary_fixture_opt_out_marker_passes(repo: Path) -> None:
    literal = _cgnat_ip(101, "102.103")
    _write(
        repo,
        "ui/app/src-tauri/src/urls.rs",
        f'assert!(is_private_host(&url("http://{literal}:3000/"))); '
        "// tailnet-ip-ok: CGNAT in-range boundary fixture\n",
    )
    assert gate._scan_file("ui/app/src-tauri/src/urls.rs") == []


def test_other_lines_with_marker_not_exempted(repo: Path) -> None:
    """The marker exempts only its own line — a second literal elsewhere in a
    marked file is still a violation."""
    boundary = _cgnat_ip(101, "102.103")
    other = _cgnat_ip(64, "0.9")
    _write(
        repo,
        "tests/fixture.py",
        f"a = 'http://{boundary}:3000'  # tailnet-ip-ok: boundary\nb = 'http://{other}:8000'\n",
    )
    hits = gate._scan_file("tests/fixture.py")
    assert len(hits) == 1
    assert hits[0][1] == other


def test_decisions_is_frozen_exemption(repo: Path) -> None:
    literal = _cgnat_ip(103, "96.72")
    _write(repo, "decisions/2026-06-11-multihost-deployment.md", f"our gateway {literal}\n")
    assert gate._scan_file("decisions/2026-06-11-multihost-deployment.md") == []


def test_binary_file_skipped(repo: Path) -> None:
    p = repo / "assets/icon.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes((_cgnat_ip(64, "0.2") + "\x00more").encode())
    assert gate._scan_file("assets/icon.bin") == []


def test_non_utf8_file_skipped(repo: Path) -> None:
    p = repo / "assets/legacy.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_cgnat_ip(64, "0.2").encode() + b"\xff\xfe")
    assert gate._scan_file("assets/legacy.txt") == []


def test_explicit_path_scan_catches_untracked_violation(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-repo scan only sees git-tracked files; an explicit path scan
    must catch a not-yet-staged edit (the pre-commit workflow's case)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gate, "_REPO_ROOT", repo)
    f = repo / "new-file.txt"
    f.write_text(f"url = 'http://{_cgnat_ip(64, '0.2')}:8000'\n", encoding="utf-8")
    assert gate.main([str(f)]) == 1


def test_tracked_scan_preserves_git_order_and_line_output(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _cgnat_ip(64, "0.2")
    second = _cgnat_ip(127, "0.3")
    _write(repo, "docs/z.md", f"clean\n  host = '{first}'  \n")
    _write(repo, "docs/a.md", f"host = '{second}'\n")
    monkeypatch.setattr(gate, "_tracked_files", lambda: ["docs/z.md", "docs/a.md"])
    assert gate.main([]) == 1
    assert capsys.readouterr().out == (
        f"docs/z.md:2: {first} | host = '{first}'\ndocs/a.md:1: {second} | host = '{second}'\n"
    )


def test_explicit_targets_are_sorted_deduplicated_and_honor_opt_out(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _cgnat_ip(64, "0.2")
    second = _cgnat_ip(127, "0.3")
    _write(repo, "docs/z.md", f"host = '{first}'\n")
    _write(repo, "docs/a.md", f"host = '{second}'  # tailnet-ip-ok: fixture\n")
    monkeypatch.setattr(gate, "_tracked_files", lambda: pytest.fail("unexpected git scan"))
    assert gate.main(["docs/z.md", "docs"]) == 1
    assert capsys.readouterr().out == f"docs/z.md:1: {first} | host = '{first}'\n"


def test_explicit_missing_target_is_an_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd explicit path must fail the gate, not pass as a silent empty scan."""
    good = repo / "ok.txt"
    good.write_text("no tailnet literals here\n", encoding="utf-8")
    missing = repo / "typo.txt"
    assert gate.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert gate.main([str(good), str(missing)]) == 1
    assert gate.main(["typo.txt"]) == 1


def test_out_of_repo_directory_argument_is_scanned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory outside the repo must be scanned: its members used to die
    on the repo-relative prefix computation."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ok.txt").write_text("no tailnet literals here\n", encoding="utf-8")
    assert gate.main([str(outside)]) == 0
    (outside / "bad.txt").write_text(
        f"url = 'http://{_cgnat_ip(64, '0.3')}:8000'\n", encoding="utf-8"
    )
    assert gate.main([str(outside)]) == 1
    assert "bad.txt" in capsys.readouterr().out
