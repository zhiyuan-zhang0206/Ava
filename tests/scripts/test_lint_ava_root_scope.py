"""scripts/lint_ava_root_scope.py: the root supervisor's scope gate.

The gate keeps services/ava_root/ free of permission-domain and
platform-specific names (ruling 2026-09-12: the privileged helper program is a
separate codebase by design, so the root supervisor's own code may not couple
to it). These tests pin the three symbol groups, the inline opt-out marker,
the skip rules (binary / non-UTF-8 / __pycache__), and the default scan root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_ava_root_scope as gate


def _write(base: Path, rel: str, content: str) -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_clean_file_passes(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "mod.py",
        '"""Unit registry for the process tree."""\n\nVALUE = "spawn"\n',
    )
    assert gate.scan_file(path) == []


@pytest.mark.parametrize(
    "symbol",
    [
        # permission-domain group
        "permissions_helper",
        "permissions-helper",
        "permissions helper",
        "PermissionsHelper",
        "TCC",
        "tccd",
        "accessibility",
        "AXUIElement",
        "screen_capture",
        "screencapture",
        "Screen Recording",
        "Quartz",
        "CGWindowListCreateImage",
        "keychain",
        "SecItem",
        "osascript",
        "AppleScript",
        "codesign",
        "notarized",
        "entitlements",
        "security find-generic-password",
        "Security.framework",
        # macOS-specific group
        "launchd",
        "launchctl",
        "LaunchAgents",
        "plist",
        "XPC",
        "Cocoa",
        "AppKit",
        "CoreFoundation",
        "Darwin",
        "macOS",
        "Mac OS X",
        "Mach",
        # other platform-mechanism group
        "systemd",
        "schtasks",
    ],
)
def test_forbidden_symbol_is_flagged(tmp_path: Path, symbol: str) -> None:
    path = _write(tmp_path, "mod.py", f'note = "{symbol}"\n')
    hits = gate.scan_file(path)
    assert len(hits) == 1
    assert hits[0][0] == 1
    # A pattern may match a prefix of the written symbol (e.g. "Mac OS" inside
    # "Mac OS X"); what matters is that the forbidden name was caught.
    assert symbol.lower().startswith(hits[0][1].lower())


def test_matching_is_case_insensitive_and_word_bounded(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "mod.py",
        "value = 'Tcc'\ncomment = 'TCC-free implementation'\nmachinery = 'a machine box'\n",
    )
    hits = gate.scan_file(path)
    assert [h[0] for h in hits] == [1, 2]


def test_plain_words_are_not_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "mod.py",
        "# A helper function spawns the next generation.\n"
        "# The security model is entry defense, not in-tree software.\n"
        "# Machinery stays generic; a machine is just a box.\n"
        "# Access to the host is out of scope here.\n",
    )
    assert gate.scan_file(path) == []


def test_inline_marker_exempts_only_its_own_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "mod.py",
        "flagged = 'TCC'  # ava-root-scope-ok: boundary fixture\nother = 'tccd'\n",
    )
    hits = gate.scan_file(path)
    assert [h[0] for h in hits] == [2]


def test_binary_file_skipped(tmp_path: Path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"launchd\x00binary")
    assert gate.scan_file(path) == []


def test_non_utf8_file_skipped(tmp_path: Path) -> None:
    path = tmp_path / "legacy.txt"
    path.write_bytes(b"launchd" + b"\xff\xfe")
    assert gate.scan_file(path) == []


def test_directory_scan_skips_pycache_and_pyc(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/real.py", "value = 1\n")
    _write(tmp_path, "pkg/__pycache__/cached.py", "value = 'launchd'\n")
    pyc = tmp_path / "pkg" / "cached.pyc"
    pyc.write_bytes(b"\x00pyc")
    files = gate._iter_files([tmp_path / "pkg"])
    assert [f.relative_to(tmp_path).as_posix() for f in files] == ["pkg/real.py"]


def test_main_returns_nonzero_on_violation(tmp_path: Path) -> None:
    bad = _write(tmp_path, "mod.py", "value = 'systemd'\n")
    assert gate.main([str(bad)]) == 1


def test_main_returns_zero_on_clean_file(tmp_path: Path) -> None:
    good = _write(tmp_path, "mod.py", "value = 'clean'\n")
    assert gate.main([str(good)]) == 0


def test_default_scan_root_is_ava_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    _write(tmp_path, "services/ava_root/ok.py", "value = 1\n")
    assert gate.main([]) == 0
    _write(tmp_path, "services/ava_root/bad.py", "value = 'launchd'\n")
    assert gate.main([]) == 1


def test_other_services_are_not_scanned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate's scope is the root supervisor's own code, not siblings."""
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    _write(tmp_path, "services/other/neighbour.py", "value = 'launchd'\n")
    assert gate.main([]) == 0


def test_repo_ava_root_is_clean() -> None:
    """The shipped root supervisor passes its own gate (self-check)."""
    assert gate.main([]) == 0
