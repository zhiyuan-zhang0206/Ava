from pathlib import Path

import pytest

from scripts.content_lint.code_comments.check import check
from scripts.content_lint.code_comments.tests.cases import files, record, repository


def test_reference_resolves_to_its_exact_owner_and_real_test(tmp_path: Path) -> None:
    repo = repository(tmp_path, files())
    result = check(repo, strict_paths=repo.files)
    assert not result.findings
    assert len(result.contracts) == 1


@pytest.mark.parametrize(
    "shebang",
    [
        "#!/usr/bin/env python",
        "#!/usr/bin/env python3",
        "#!/usr/bin/env python3.12",
        "#!/usr/bin/python3",
        "#!/usr/bin/python3.12",
    ],
)
def test_python_entry_shebang_is_syntax(tmp_path: Path, shebang: str) -> None:
    source = files()
    source["pkg/core.py"] = shebang + "\n" + source["pkg/core.py"]
    repo = repository(tmp_path, source)
    assert not check(repo, strict_paths=repo.files).findings


@pytest.mark.parametrize(
    "prefix",
    [
        "#!/usr/bin/env python because it is needed\n",
        "#!/usr/bin/env python # a reason\n",
        "#!/usr/bin/env python -u\n",
        "#!/usr/bin/env -S python\n",
        "#!/usr/bin/env python2\n",
        "#!/usr/bin/env python3.13\n",
        "#!/usr/bin/python\n",
        "#!/usr/local/bin/python\n",
        "#!/usr/bin/env pythons\n",
        "# !/usr/bin/env python\n",
        " #!/usr/bin/env python\n",
        "\n#!/usr/bin/env python\n",
    ],
)
def test_python_shebang_does_not_hide_comments(tmp_path: Path, prefix: str) -> None:
    source = files()
    source["pkg/core.py"] = prefix + source["pkg/core.py"]
    repo = repository(tmp_path, source)
    findings = check(repo, strict_paths=repo.files).findings
    assert len(findings) == 1
    assert findings[0].code == "free-comment"
    assert findings[0].path == "pkg/core.py"


@pytest.mark.parametrize(
    "comment",
    [
        "# WHY: necessary",
        "# type: ignore",
        "# noqa: F821",
        "# lint-docstring: ok required",
        "# ava:ref pkg/docs/core.ava.okf.md#positive-budget because it is needed",
    ],
)
def test_free_text_and_suppressions_are_not_escape_hatches(tmp_path: Path, comment: str) -> None:
    source = files()
    source["pkg/core.py"] += comment + "\n"
    repo = repository(tmp_path, source)
    assert any(
        item.code == "free-comment" for item in check(repo, strict_paths=repo.files).findings
    )


def test_comment_shaped_runtime_strings_are_not_comments(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] += "message = '# a value, not a comment'\n"
    repo = repository(tmp_path, source)
    assert not check(repo, strict_paths=repo.files).findings


@pytest.mark.parametrize(
    "extra",
    [
        '"""An implementation note."""\n',
        'def hidden():\n    """Implementation notes."""\n    return 2\n',
    ],
)
def test_docstrings_and_unused_strings_cannot_hide_notes(tmp_path: Path, extra: str) -> None:
    source = files()
    source["pkg/core.py"] += extra
    repo = repository(tmp_path, source)
    assert any(
        item.code == "free-comment" for item in check(repo, strict_paths=repo.files).findings
    )


def test_a_real_class_does_not_make_its_missing_method_resolve(tmp_path: Path) -> None:
    source = files(code=["pkg/core.py:Worker.missing"])
    source["pkg/core.py"] = "class Worker:\n    def present(self):\n        return 1\n"
    result = check(repository(tmp_path, source))
    assert any(
        "Worker.missing" in item.message and item.code == "contract-binding"
        for item in result.findings
    )


def test_a_value_named_like_a_test_is_not_test_evidence(tmp_path: Path) -> None:
    source = files()
    source["pkg/tests/test_core.py"] = "test_budget = 'not callable'\n"
    assert any(
        item.code == "contract-binding" for item in check(repository(tmp_path, source)).findings
    )


def test_removing_test_or_symbol_invalidates_unchanged_document(tmp_path: Path) -> None:
    source = files()
    source["pkg/tests/test_core.py"] = "def test_other():\n    assert True\n"
    result = check(repository(tmp_path, source))
    assert any("test_budget" in item.message for item in result.findings)


def test_references_cannot_attach_to_an_unrelated_function(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = (
        "def budget():\n    return 1\n\n# ava:ref pkg/docs/core.ava.okf.md#positive-budget\ndef unrelated():\n    return 2\n"
    )
    assert any(
        item.code == "comment-owner" for item in check(repository(tmp_path, source)).findings
    )


def test_unused_records_and_duplicate_ids_fail(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = "def budget():\n    return 1\n"
    source["pkg/docs/core.ava.okf.md"] += record()
    codes = {item.code for item in check(repository(tmp_path, source)).findings}
    assert {"contract-duplicate", "contract-unused"} <= codes


def test_references_do_not_escape_the_repository_or_document_owner(tmp_path: Path) -> None:
    for number, anchor in enumerate(
        ["../outside.py:budget", "/outside.py:budget", "elsewhere/core.py:budget"]
    ):
        source = files(code=[anchor])
        source["elsewhere/core.py"] = "def budget(): return 1\n"
        result = check(repository(tmp_path / str(number), source))
        assert any(item.code == "contract-binding" for item in result.findings)


def test_symlinked_source_cannot_supply_a_binding(tmp_path: Path) -> None:
    repo = repository(tmp_path, files())
    (tmp_path / "pkg/core.py").unlink()
    (tmp_path / "pkg/core.py").symlink_to(tmp_path / "pkg/tests/test_core.py")
    assert any(item.code == "contract-binding" for item in check(repo).findings)


def test_validation_does_not_claim_to_have_completed_migration(tmp_path: Path) -> None:
    source = files()
    source["legacy.py"] = "# Unmigrated source.\n"
    repo = repository(tmp_path, source)
    assert not check(repo).findings
    assert any(
        item.path == "legacy.py" and item.code == "free-comment"
        for item in check(repo, strict_paths=repo.files).findings
    )
