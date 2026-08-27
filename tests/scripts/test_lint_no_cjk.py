"""scripts/lint_no_cjk.py: the repo-wide no-CJK gate.

User ruling 2026-08-27 (tightening the 2026-08-06 English-primary rule): raw
CJK characters are banned everywhere in the repo; the only exemption is i18n /
locale copy (message catalogs, locale trees, and the IM alert-copy locale
module). These tests pin the detection, the exemption paths, and the
ASCII-escape convention for functional CJK data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_no_cjk as gate


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the gate at a scratch repo root so the real checkout is untouched."""
    monkeypatch.setattr(gate, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        gate,
        "_LOCALE_PY_FILES",
        frozenset({"shared/alerts_copy.py", "shared/pages_copy.py"}),
    )
    return tmp_path


def _write(repo: Path, rel: str, content: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_english_file_passes(repo: Path) -> None:
    _write(repo, "docs/guide.md", "# Guide\n\nAll English here.\n")
    assert gate._scan_file("docs/guide.md") == []


def test_chinese_ideograph_fails(repo: Path) -> None:
    _write(repo, "docs/guide.md", "# Guide\n\u4e2d\u6587 text\n")
    hits = gate._scan_file("docs/guide.md")
    assert len(hits) == 1
    assert hits[0][0] == 2
    assert hits[0][1] == "\u4e2d"


def test_kana_and_hangul_fail(repo: Path) -> None:
    _write(repo, "docs/guide.md", "# Guide\n\u3059\u30ad\u30eb\n")
    assert gate._scan_file("docs/guide.md")
    _write(repo, "docs/guide.md", "# Guide\n\uc778\uc99d\n")
    assert gate._scan_file("docs/guide.md")


def test_fullwidth_punctuation_fails(repo: Path) -> None:
    """Real CJK text always carries fullwidth punctuation, so the gate covers
    CJK symbols/punctuation and fullwidth forms too - a fullwidth comma alone
    is Chinese punctuation and fails on its own."""
    _write(repo, "docs/guide.md", "# Guide\nhello\uff0cworld\n")
    hits = gate._scan_file("docs/guide.md")
    assert len(hits) == 1 and hits[0][1] == "\uff0c"
    _write(repo, "docs/guide.md", "# Guide\n\u4f60\u597d\uff0c\u4e16\u754c\n")
    assert gate._scan_file("docs/guide.md")
    _write(repo, "docs/guide.md", "# Guide\n\u300cquote\u300d\n")
    assert gate._scan_file("docs/guide.md")


def test_binary_file_skipped(repo: Path) -> None:
    _write(repo, "assets/logo.png", "\x00\x01\x02binary")
    assert gate._scan_file("assets/logo.png") == []


def test_next_intl_messages_catalog_exempt(repo: Path) -> None:
    """The frontend's zh message catalog is the ruling's explicit exemption."""
    _write(repo, "ui/web/messages/zh.json", '{"common": {"save": "\u4fdd\u5b58"}}\n')
    assert gate._scan_file("ui/web/messages/zh.json") == []


def test_locales_dir_and_po_exempt(repo: Path) -> None:
    _write(repo, "frontend/locales/zh/messages.po", "msgstr \u4fdd\u5b58\n")
    assert gate._scan_file("frontend/locales/zh/messages.po") == []
    _write(repo, "frontend/locales/zh/app.json", '{"ok": "\u597d"}\n')
    assert gate._scan_file("frontend/locales/zh/app.json") == []


def test_alerts_copy_locale_module_exempt(repo: Path) -> None:
    """shared/alerts_copy.py is the IM alert copy locale module (zh/en by
    display.language) - the one Python locale file, documented in the gate."""
    _write(repo, "shared/alerts_copy.py", 'ALERT_HEAD = {"zh": "\u544a\u8b66"}\n')
    assert gate._scan_file("shared/alerts_copy.py") == []


def test_pages_copy_locale_module_exempt(repo: Path) -> None:
    """shared/pages_copy.py is the page-expired copy locale module (zh/en by
    display.language) - same exemption class as alerts_copy."""
    _write(
        repo,
        "shared/pages_copy.py",
        'PAGE_EXPIRED_BODY = {"zh": "\u9875\u9762\u5df2\u8fc7\u671f"}\n',
    )
    assert gate._scan_file("shared/pages_copy.py") == []


def test_skill_body_with_cjk_fails(repo: Path) -> None:
    """Skill bodies are in scope - the ruling names skill content explicitly."""
    _write(repo, "ava_builtins/skills/pkg/SKILL.md", "# Skill\n\u4e2d\u6587\n")
    assert gate._scan_file("ava_builtins/skills/pkg/SKILL.md")


def test_main_returns_1_on_hits_and_0_when_clean(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo, "docs/a.md", "clean\n")
    monkeypatch.setattr(gate, "_tracked_files", lambda: ["docs/a.md"])
    assert gate.main([]) == 0
    _write(repo, "docs/a.md", "\u4e2d\u6587\n")
    assert gate.main([]) == 1


def test_explicit_paths_scan_untracked_edits(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-commit runs whole-repo, but an explicit path must catch a CJK edit
    even before `git add` (the tracked-file list would miss it)."""
    _write(repo, "new.txt", "\u4e2d\u6587\n")
    monkeypatch.setattr(gate, "_tracked_files", list)
    assert gate.main(["new.txt"]) == 1
