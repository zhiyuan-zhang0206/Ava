import hashlib
import json
from pathlib import Path

import pytest

from scripts.content_lint.code_comments.tests.cases import repository
from scripts.content_lint.code_comments.typescript_index import load_index


def parser_output() -> dict[str, object]:
    return {
        "version": 1,
        "sources": [
            {
                "path": "core.ts",
                "sha256": hashlib.sha256(b"export const value = 1;\n").hexdigest(),
                "symbols": {
                    "<module>": {
                        "name": "<module>",
                        "line": 1,
                        "end_line": 2,
                        "parameters": [],
                        "ambiguous": False,
                        "callable": False,
                    }
                },
                "notes": [],
            }
        ],
    }


def test_parser_output_must_match_the_current_source(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"core.ts": "export const value = 1;\n"})
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
    repo = repository(tmp_path, {"core.ts": "export const value = 1;\n"})
    data = parser_output()
    data["sources"] = replacement
    with pytest.raises(ValueError):
        load_index(repo, json.dumps(data))


def test_unknown_parser_version_and_fields_fail(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"core.ts": "export const value = 1;\n"})
    for key, value in [("version", 2), ("skip", True)]:
        data = parser_output()
        data[key] = value
        with pytest.raises(ValueError):
            load_index(repo, json.dumps(data))
