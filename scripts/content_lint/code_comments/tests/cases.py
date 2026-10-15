import json
from pathlib import Path

from scripts.content_lint.code_comments.check import Repository


def repository(root: Path, files: dict[str, str]) -> Repository:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Repository(root, frozenset(files))


def record(**fields: object) -> str:
    values = {
        "id": "positive-budget",
        "kind": "invariant",
        "statement": "The budget is positive.",
        "code": ["pkg/core.py:budget"],
        "tests": ["pkg/tests/test_core.py::test_budget"],
        **fields,
    }
    return (
        "```ava-contract\n"
        + "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
        + "\n```\n"
    )


def files(**fields: object) -> dict[str, str]:
    return {
        "pkg/core.py": "# ava:ref pkg/docs/core.ava.okf.md#positive-budget\ndef budget():\n    return 1\n",
        "pkg/docs/core.ava.okf.md": record(**fields),
        "pkg/tests/test_core.py": "def test_budget():\n    assert 1 > 0\n",
    }
