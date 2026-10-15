import pytest

from scripts.content_lint.code_comments.model import Kind
from scripts.content_lint.code_comments.records import parse_document
from scripts.content_lint.code_comments.tests.cases import record


def test_contracts_have_closed_types_and_kind_specific_fields() -> None:
    parsed, findings = parse_document("pkg/docs/core.ava.okf.md", record())
    assert not findings
    assert parsed[0].kind is Kind.INVARIANT
    assert parsed[0].owner == "pkg"


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "note"},
        {"approved": True},
        {"ignore": "necessary"},
        {"statement": " "},
        {"tests": []},
        {"tests": ["x", "x"]},
        {"code": []},
        {"id": "UPPER"},
        {"kind": "external"},
        {"kind": "external", "source": "file:///tmp/spec", "applies_to": "v1"},
        {"kind": "external", "source": "https://user:pass@example.org", "applies_to": "v1"},
        {"kind": "decision", "decision": "docs/decisions/../conventions/rules.md"},
        {"kind": "api", "api": "ava.files.read", "code": ["a.py:a", "b.py:b"]},
    ],
)
def test_malformed_records_cannot_grant_an_exception(fields: dict[str, object]) -> None:
    parsed, findings = parse_document("pkg/docs/core.ava.okf.md", record(**fields))
    assert not parsed
    assert len(findings) == 1
    assert findings[0].code == "contract-schema"


def test_examples_inside_an_outer_fence_are_not_live_records() -> None:
    parsed, findings = parse_document(
        "docs/conventions/comments.md", "````markdown\n" + record() + "````\n"
    )
    assert not parsed
    assert not findings


@pytest.mark.parametrize(
    "path", ["pkg/README.md", "future/docs/core.ava.okf.md", "docs/decisions/core.ava.okf.md"]
)
def test_current_contracts_cannot_hide_in_ungoverned_or_historical_prose(path: str) -> None:
    parsed, findings = parse_document(path, record())
    assert not parsed
    assert findings


def test_unclosed_or_option_bearing_fences_fail() -> None:
    for text in [
        record().rsplit("```", 1)[0],
        record().replace("```ava-contract", "```ava-contract ignore=true"),
    ]:
        _, findings = parse_document("pkg/docs/core.ava.okf.md", text)
        assert findings
