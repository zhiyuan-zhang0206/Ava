from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint import ava_root_scope as gate


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


def test_architecture_docs_can_describe_platform_boundaries(tmp_path: Path) -> None:
    doc = _write(
        tmp_path,
        "root.ava.okf.md",
        "macOS: launchd -> permissions helper -> root\nLinux: systemd -> root\n",
    )
    code = _write(
        tmp_path,
        "root.py",
        '"""Linux systemd owns root; macOS uses the permissions helper."""\n\ndef serve():\n    """This root does not call launchctl."""\n    return "launchctl"\n',
    )
    assert gate.scan_file(doc) == []
    assert [(line, symbol) for line, symbol, _text in gate.scan_file(code)] == [(5, "launchctl")]


def test_docstring_cannot_hide_executable_statement_on_same_line(tmp_path: Path) -> None:
    path = _write(tmp_path, "root.py", '"""Architecture."""; command = "launchctl"\n')
    assert [(line, symbol) for line, symbol, _text in gate.scan_file(path)] == [(1, "launchctl")]


@pytest.mark.parametrize(
    "symbol",
    [
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
        "systemd",
    ],
)
def test_forbidden_symbol_is_flagged(tmp_path: Path, symbol: str) -> None:
    path = _write(tmp_path, "mod.py", f'note = "{symbol}"\n')
    hits = gate.scan_file(path)
    assert len(hits) == 1
    assert hits[0][0] == 1
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


def test_inline_marker_in_string_literal_does_not_exempt(tmp_path: Path) -> None:
    path = _write(tmp_path, "mod.py", 'marker = "ava-root-scope-ok: TCC"\n')
    hits = gate.scan_file(path)
    assert [h[0] for h in hits] == [1]


def test_unparseable_text_falls_back_to_substring_marker(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "mod.py",
        "first = 'ava-root-scope-ok: TCC'\nsecond = 'open quote\n",
    )
    assert gate.scan_file(path) == []


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
    files = gate.iter_files([tmp_path / "pkg"])
    assert [f.relative_to(tmp_path).as_posix() for f in files] == ["pkg/real.py"]


def test_main_returns_nonzero_on_violation(tmp_path: Path) -> None:
    bad = _write(tmp_path, "mod.py", "value = 'systemd'\n")
    assert gate.main([str(bad)]) == 1


def test_main_returns_zero_on_clean_file(tmp_path: Path) -> None:
    good = _write(tmp_path, "mod.py", "value = 'clean'\n")
    assert gate.main([str(good)]) == 0


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _write(tmp_path, "mod.py", "value = 1\n")
    missing = tmp_path / "typo.py"
    assert gate.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert gate.main([str(good), str(missing)]) == 1


def test_default_scan_root_is_ava_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    _write(tmp_path, "services/supervision/ava_root/ok.py", "value = 1\n")
    assert gate.main([]) == 0
    _write(tmp_path, "services/supervision/ava_root/bad.py", "value = 'launchd'\n")
    assert gate.main([]) == 1


def test_other_services_are_not_scanned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    _write(tmp_path, "services/other/neighbour.py", "value = 'launchd'\n")
    assert gate.main([]) == 0
