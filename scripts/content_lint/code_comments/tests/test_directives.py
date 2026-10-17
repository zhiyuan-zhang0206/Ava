from pathlib import Path

import pytest

from scripts.content_lint.code_comments.check import Repository, check
from scripts.content_lint.code_comments.tests.cases import DOC, files, note, repository
from scripts.content_lint.lint_quiesced_loops import violations_in_source

RATIONALE = note(("The caller already proves the value.",), links=(DOC,))


def codes(repo: Repository) -> list[str]:
    return [item.code for item in check(repo, strict_paths=repo.files).findings]


def source_with(*parts: str) -> dict[str, str]:
    return {**files(), "pkg/core.py": "".join(parts)}


@pytest.mark.parametrize(
    "directive",
    [
        "# noqa: S603",
        "# noqa: S106, ARG002",
        "# noqa: PLR0913, PLR2004",
        "# pyright: ignore[reportArgumentType]",
        "# pyright: ignore[reportArgumentType, reportUnknownMemberType]",
    ],
)
def test_a_line_directive_uses_the_note_owned_by_its_definition(
    tmp_path: Path, directive: str
) -> None:
    body = f"    value = call(  {directive}\n        1,\n    )\n    return value\n"
    repo = repository(tmp_path, source_with(RATIONALE, "def budget():\n", body))
    assert codes(repo) == []


@pytest.mark.parametrize(
    "directive",
    [
        "# noqa",
        "# ruff: noqa: S603",
        "# noqa: S603 because input is trusted",
        "# noqa: S603 # another comment",
        "# noqa: S603,",
        "# noqa: E4",
        "# pyright: ignore",
        "# pyright: ignore[reportArgumentType] because it is needed",
        "# pyright: reportArgumentType=false",
        "# type: ignore[arg-type]",
    ],
)
def test_unbounded_or_prose_directives_stay_rejected(tmp_path: Path, directive: str) -> None:
    repo = repository(
        tmp_path, source_with(RATIONALE, "def budget():\n    return call()  " + directive + "\n")
    )
    assert codes(repo) == ["free-comment"]


def test_a_directive_without_a_bound_note_fails(tmp_path: Path) -> None:
    repo = repository(tmp_path, {"pkg/core.py": "def budget():\n    return call()  # noqa: S603\n"})
    assert codes(repo) == ["directive-owner"]


@pytest.mark.parametrize(
    "body",
    [
        RATIONALE
        + "def budget():\n    return 1\n\ndef other():\n    return call()  # noqa: S603\n",
        RATIONALE
        + "def budget():\n    def nested():\n        return call()  # noqa: S603\n"
        + "    return nested()\n",
        note(("Module fact.",), links=(DOC,))
        + "\ndef budget():\n    return call()  # noqa: S603\n",
    ],
)
def test_a_directive_cannot_borrow_sibling_outer_or_module_rationale(
    tmp_path: Path, body: str
) -> None:
    repo = repository(tmp_path, source_with(body))
    assert codes(repo) == ["directive-owner"]


def test_a_class_note_does_not_cover_its_method_directives(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        source_with(
            "class Worker:\n",
            note(("The protocol requires the parameter.",), links=(DOC,), indent=4),
            "    def budget(self, value: bool = True):  # noqa: ARG002\n        return None\n",
        ),
    )
    assert codes(repo) == ["directive-owner"]


def test_a_method_note_covers_its_method_directives(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        source_with(
            "class Worker:\n",
            "    def budget(self, value: bool = True):  # noqa: ARG002\n",
            note(("The protocol requires the parameter.",), links=(DOC,), indent=8),
            "        return None\n",
        ),
    )
    assert codes(repo) == []


def test_signature_and_definition_directives_bind_the_definition(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        source_with(
            RATIONALE,
            "def budget(\n    value: bool = True,  # noqa: ARG002\n"
            ") -> None:  # pyright: ignore[reportIncompatibleMethodOverride]\n    return None\n",
        ),
    )
    assert codes(repo) == []


def test_an_unbound_directive_outside_the_strict_scope_is_not_reported(
    tmp_path: Path,
) -> None:
    repo = repository(
        tmp_path,
        source_with(
            RATIONALE,
            "def budget():\n    return 1\n\ndef other():\n    return call()  # noqa: S603\n",
        ),
    )
    assert check(repo).findings == []


def test_a_duplicate_definition_cannot_borrow_its_neighbours_rationale(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = note(("The first definition keeps a budget.",), links=(DOC,)) + (
        "def budget() -> int:\n    return 1\n\n\n"
        "def budget() -> int:\n    return call()  # noqa: S603\n"
    )
    assert codes(repository(tmp_path, source)) == ["directive-owner"]


def test_a_link_only_note_points_at_the_rationale_document(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        source_with(note((), links=(DOC,)), "def budget():\n    return call()  # noqa: S603\n"),
    )
    assert codes(repo) == []


def test_a_bare_quiesce_marker_is_not_machine_syntax(tmp_path: Path) -> None:
    loop = "    # quiesce-exempt\n    while True:\n        sleep(1)\n"
    bound = repository(tmp_path / "bound", source_with(RATIONALE + "def loop():\n", loop))
    assert codes(bound) == ["free-comment"]
    unbound = repository(tmp_path / "unbound", {"pkg/core.py": "def loop():\n" + loop})
    assert codes(unbound) == ["free-comment"]


def test_a_quiesce_marker_with_its_own_reason_is_machine_syntax(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        source_with(
            RATIONALE,
            "def loop():\n    # quiesce-exempt: never touches the database\n"
            "    while True:\n        sleep(1)\n",
        ),
    )
    assert codes(repo) == []


def test_the_stop_window_lint_keeps_its_own_marker_requirement() -> None:
    marker = "    # quiesce-exempt: never touches the database\n"
    loop = "    while True:\n        sleep(1)\n"
    assert not violations_in_source("def budget():\n" + marker + loop)
    assert violations_in_source("def budget():\n" + loop)
    misplaced = "def budget():\n" + marker + "    value = 1\n" + loop
    assert any("stale" in message for _, message in violations_in_source(misplaced))


@pytest.mark.parametrize("path", ["pkg/core.py", "pkg/tests/test_core.py"])
@pytest.mark.parametrize("call", ["gateway_access_from_settings()", "other()"])
def test_an_ambient_marker_only_names_an_audited_call(tmp_path: Path, path: str, call: str) -> None:
    repo = repository(tmp_path, {path: f"access = {call}  # ambient: B2\n"})
    findings = check(repo, strict_paths=repo.files).findings
    assert bool(findings) is (path != "pkg/core.py" or call != "gateway_access_from_settings()")


def test_an_ambient_marker_cannot_include_prose(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        {"pkg/core.py": "access = gateway_access_from_settings()  # ambient: B2 because needed\n"},
    )
    assert [item.code for item in check(repo, strict_paths=repo.files).findings] == ["free-comment"]
