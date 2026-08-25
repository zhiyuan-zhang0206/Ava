"""scripts/lint_code_structure.py — the _OVERSIZE_ALLOWED exemption lifecycle.

Locks the 2026-08-24 tech-audit P2 contract: an oversized-file exemption
carries (owner, target, expiry), and the lint enforces both failure modes —
an EXPIRED exemption is a hard error (renew or split), and a STALE one (a
listed file now under the hard ceiling) is a hard error too (the list must
match reality).
"""

from __future__ import annotations

import pathlib

import pytest

from scripts import lint_code_structure as lcs


def _write(tmp_path: pathlib.Path, name: str, n_lines: int) -> pathlib.Path:
    p = tmp_path / name
    p.write_text("x = 1\n" * n_lines, encoding="utf-8")
    return p


def _scan(tmp_path: pathlib.Path, name: str, n_lines: int) -> list[str]:
    p = _write(tmp_path, name, n_lines)
    return [msg for _ln, msg, sev in lcs._scan_file(p, name) if sev == "error"]


def test_oversize_allowlisted_file_passes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listed file over the ceiling with a live exemption is not an error."""
    monkeypatch.setattr(lcs, "_OVERSIZE_ALLOWED", {"big.py": ("#1", 500, "2099-01-01")})
    assert _scan(tmp_path, "big.py", 900) == []


def test_expired_exemption_is_hard_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exemption past its expiry fails the run — renew with a current
    justification or split the file."""
    monkeypatch.setattr(lcs, "_OVERSIZE_ALLOWED", {"big.py": ("#1", 500, "2020-01-01")})
    errors = _scan(tmp_path, "big.py", 900)
    assert any("expired" in e for e in errors)


def test_stale_exemption_is_hard_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listed file now under the hard ceiling must leave the list — the
    same stale-entry discipline as the machine_role() allowlist."""
    monkeypatch.setattr(lcs, "_OVERSIZE_ALLOWED", {"small.py": ("#1", 500, "2099-01-01")})
    errors = _scan(tmp_path, "small.py", 100)
    assert any("stale" in e for e in errors)


def test_unlisted_oversize_is_hard_error(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-ceiling file with no exemption is the pre-existing hard error."""
    monkeypatch.setattr(lcs, "_OVERSIZE_ALLOWED", {})
    errors = _scan(tmp_path, "big.py", 900)
    assert any("hard ceiling" in e for e in errors)
