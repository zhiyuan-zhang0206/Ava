from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.content_lint import lint_panel_jargon as gate

_EVIDENCE_HEADING = "\u8be6\u7ec6\u8bc1\u636e"
_GLOSSARY_HEADING = "**\u540d\u8bcd\u8868**\uff1a"
_GLOSSARY_TABLE = "| \u8bcd | \u610f\u601d |\n|---|---|\n"
_BODY_OK = "# \u72b6\u6001\n\n\u4fee\u597d\u4e86\u4e00\u4e2a\u754c\u9762\u95ee\u9898\uff0c\u6539\u52a8\u5df2\u5408\u5165\u4e3b\u5e72\u3002"
_BODY_WITH_ID = "# \u72b6\u6001\n\n\u6539\u52a8 P1 \u5df2\u5408\u5e76\u3002"


@pytest.fixture
def rules(tmp_path: Path) -> Path:
    """Use the shipped rule file unchanged."""
    path = tmp_path / "rules.toml"
    path.write_text(gate.DEFAULT_RULES.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _panel(
    tmp_path: Path, body: str, evidence: str = "- \u6539\u52a8 P1", glossary: str = ""
) -> Path:
    text = body
    if glossary:
        text += f"\n\n{_GLOSSARY_HEADING}\n\n{_GLOSSARY_TABLE}{glossary}\n"
    text += f"\n\n## {_EVIDENCE_HEADING}\n\n{evidence}\n"
    path = tmp_path / "to_user.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_clean_body_passes(tmp_path: Path, rules: Path) -> None:
    panel = _panel(tmp_path, _BODY_OK)
    findings, entries, passed = gate.evaluate(panel, gate.load_ruleset(rules))
    assert (findings, entries, passed) == ([], 0, True)


def test_bare_identifier_in_body_is_hard(tmp_path: Path, rules: Path) -> None:
    panel = _panel(tmp_path, _BODY_WITH_ID)
    findings, _entries, passed = gate.evaluate(panel, gate.load_ruleset(rules))
    assert not passed
    assert [finding.rule for finding in findings] == ["bare-identifier"]
    assert findings[0].hard


def test_identifiers_in_the_evidence_area_are_allowed(tmp_path: Path, rules: Path) -> None:
    panel = _panel(
        tmp_path, _BODY_OK, evidence="- \u6539\u52a8\uff08P1\uff09\u5df2\u5408\u5165 main"
    )
    findings, _entries, passed = gate.evaluate(panel, gate.load_ruleset(rules))
    assert (findings, passed) == ([], True)


def test_body_budget_uses_the_configured_limit(tmp_path: Path, rules: Path) -> None:
    line = "\u8c03\u7528\u4e86 SSE \u63a5\u53e3"
    panel = _panel(tmp_path, "# \u72b6\u6001\n\n" + "\n".join([line] * 5))
    findings, _entries, passed = gate.evaluate(panel, gate.load_ruleset(rules))
    assert len(findings) == 5 and passed
    panel = _panel(tmp_path, "# \u72b6\u6001\n\n" + "\n".join([line] * 6))
    findings, _entries, passed = gate.evaluate(panel, gate.load_ruleset(rules))
    assert len(findings) == 6 and not passed


def test_glossary_entries_are_counted_and_bounded(tmp_path: Path, rules: Path) -> None:
    entries = "\n".join(f"| \u672f\u8bed{i} | \u610f\u601d{i} |" for i in range(9))
    panel = _panel(tmp_path, _BODY_OK, glossary=entries)
    _findings, count, passed = gate.evaluate(panel, gate.load_ruleset(rules))
    assert count == 9
    assert not passed


def test_allowed_acronyms_and_glossary_terms_do_not_fire(tmp_path: Path, rules: Path) -> None:
    body = "# \u72b6\u6001\n\n\u901a\u8fc7 CI \u8dd1\u5b8c\u3002AB \u672a\u767b\u8bb0\u3002"
    panel = _panel(tmp_path, body, glossary="| AB | \u67d0\u4e2a\u7f29\u5199 |")
    findings, _entries, passed = gate.evaluate(panel, gate.load_ruleset(rules))
    assert findings == [] and passed


def test_directory_scan_finds_only_panels(tmp_path: Path, rules: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "to_user.md").write_text(_BODY_OK, encoding="utf-8")
    (tmp_path / "a" / "notes.md").write_text("P1 P2", encoding="utf-8")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "to_user.md").write_text(_BODY_WITH_ID, encoding="utf-8")
    assert gate.main([str(tmp_path), "--rules", str(rules)]) == 1


def test_json_output_reports_each_panel(
    tmp_path: Path, rules: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    panel = _panel(tmp_path, _BODY_OK)
    code = gate.main([str(panel), "--rules", str(rules), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload[0]["passed"] is True and payload[0]["panel"] == str(panel)
