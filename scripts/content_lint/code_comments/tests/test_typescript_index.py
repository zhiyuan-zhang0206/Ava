import hashlib
import json
from pathlib import Path

import pytest

from scripts.content_lint.code_comments.changes import parse_blobs
from scripts.content_lint.code_comments.check import check
from scripts.content_lint.code_comments.model import NoteKind
from scripts.content_lint.code_comments.tests.cases import repository
from scripts.content_lint.code_comments.typescript_index import load_index

SOURCE = "export const value = 1;\n"


def module_symbol() -> dict[str, object]:
    return {
        "name": "<module>",
        "line": 1,
        "end_line": 2,
        "callable": False,
    }


def parser_output(
    notes: list[dict[str, object]] | None = None,
    symbols: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "version": 2,
        "sources": [
            {
                "path": "core.ts",
                "sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
                "symbols": {"<module>": module_symbol()} if symbols is None else symbols,
                "notes": notes or [],
            }
        ],
    }


def note_record(**fields: object) -> dict[str, object]:
    return {
        "kind": "note",
        "line": 1,
        "end_line": 2,
        "text": "Retains the last value.",
        "owner": "<module>",
        "lexical_owner": "<module>",
        "raw_text": "// meiva:note\n// Retains the last value.",
        "links": ["docs/core.md"],
        **fields,
    }


def test_parser_output_must_match_the_current_source(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"core.ts": SOURCE})
    text = json.dumps(parser_output())
    load_index(repo, text)
    assert "core.ts" in repo.sources
    (tmp_path / "core.ts").write_text("// New prose\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        load_index(repo, text)


@pytest.mark.parametrize("replacement", [[], [{"path": "core.ts"}]])
def test_missing_source_or_partial_output_cannot_pass(
    tmp_path: Path, replacement: list[object]
) -> None:
    repo = repository(tmp_path, {"core.ts": SOURCE})
    data = parser_output()
    data["sources"] = replacement
    with pytest.raises(ValueError):
        load_index(repo, json.dumps(data))


def test_unknown_parser_version_and_fields_fail(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"core.ts": SOURCE})
    for key, value in [("version", 3), ("skip", True)]:
        data = parser_output()
        data[key] = value
        with pytest.raises(ValueError):
            load_index(repo, json.dumps(data))


def test_parser_notes_carry_the_block_text_ownership_and_links(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"core.ts": SOURCE, "docs/core.md": "# Core\n"})
    load_index(repo, json.dumps(parser_output([note_record()])))
    (entry,) = repo.source("core.ts").notes
    assert entry.kind is NoteKind.NOTE
    assert (entry.line, entry.end_line, entry.text) == (1, 2, "Retains the last value.")
    assert entry.owner == "<module>"
    assert entry.links == ("docs/core.md",)


@pytest.mark.parametrize(
    "fields",
    [
        {"lexical_owner": ""},
        {"raw_text": ""},
        {"owner": "missing"},
        {"kind": "machine"},
        {"kind": "lint-directive"},
        {"end_line": 0},
    ],
)
def test_a_note_record_outside_the_typescript_vocabulary_fails(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    repo = repository(tmp_path, {"core.ts": SOURCE})
    with pytest.raises(ValueError):
        load_index(repo, json.dumps(parser_output([note_record(**fields)])))


@pytest.mark.parametrize(
    "symbols",
    [
        {},
        {"<module>": {**module_symbol(), "name": "other"}},
        {"<module>": {**module_symbol(), "end_line": 0}},
    ],
)
def test_parser_symbols_must_match_their_keys(tmp_path: Path, symbols: dict[str, object]) -> None:
    repo = repository(tmp_path, {"core.ts": SOURCE})
    with pytest.raises(ValueError):
        load_index(repo, json.dumps(parser_output(symbols=symbols)))


def test_an_unparsed_typescript_source_fails_the_strict_check(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"ui/core.ts": SOURCE})
    findings = check(repo, strict_paths=repo.files).findings
    assert [(item.path, item.code) for item in findings] == [("ui/core.ts", "source-parse")]


def test_parsed_typescript_sources_feed_the_strict_check(tmp_path: Path) -> None:
    contents = {
        "ui/core.ts": (
            "// meiva:note\n// Retains the last value.\n// meiva:link ui/docs/core.md\n"
            "export const value = 1;\n"
        ),
        "ui/legacy.ts": "// free prose\nexport const other = 2;\n",
        "ui/docs/core.md": "# Core\n",
    }
    repo = repository(tmp_path, contents)
    scripts = {path: text for path, text in contents.items() if path.endswith(".ts")}
    repo.sources.update(parse_blobs(tmp_path, scripts))
    findings = check(repo, strict_paths=repo.files).findings
    assert [(item.path, item.code) for item in findings] == [("ui/legacy.ts", "free-comment")]
