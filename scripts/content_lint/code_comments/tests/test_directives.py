from pathlib import Path

import pytest

from scripts.content_lint.code_comments.check import check
from scripts.content_lint.code_comments.tests.cases import files, repository
from scripts.content_lint.lint_quiesced_loops import violations_in_source

REF = "# meiva:ref pkg/docs/core.meiva.okf.md#positive-budget"


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
def test_line_directive_uses_its_definition_contract(tmp_path: Path, directive: str) -> None:
    source = files()
    source["pkg/core.py"] = (
        f"{REF}\ndef budget():\n    result = call(  {directive}\n        1,\n    )\n    return result\n"
    )
    repo = repository(tmp_path, source)
    assert not check(repo, strict_paths=repo.files).findings


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
    source = files()
    source["pkg/core.py"] = f"{REF}\ndef budget():\n    return call()  {directive}\n"
    repo = repository(tmp_path, source)
    assert any(f.code == "free-comment" for f in check(repo, strict_paths=repo.files).findings)


@pytest.mark.parametrize(
    "body",
    [
        f"{REF}\ndef budget():\n    return 1\n\ndef other():\n    return call()  # noqa: S603\n",
        f"{REF}\ndef budget():\n    def nested():\n        return call()  # noqa: S603\n    return nested()\n",
    ],
)
def test_directive_cannot_borrow_sibling_or_outer_rationale(tmp_path: Path, body: str) -> None:
    source = files()
    source["pkg/core.py"] = body
    repo = repository(tmp_path, source)
    assert any(f.code == "directive-owner" for f in check(repo, strict_paths=repo.files).findings)


def test_signature_and_method_directives_bind_the_method(tmp_path: Path) -> None:
    source = files(code=["pkg/core.py:Worker.budget"])
    source["pkg/core.py"] = (
        "class Worker:\n"
        "    def budget(\n"
        "        self, value: bool = True,  # noqa: ARG002\n"
        "    ):  # pyright: ignore[reportIncompatibleMethodOverride]\n"
        f"        {REF}\n"
        "        return 1\n"
    )
    repo = repository(tmp_path, source)
    assert not check(repo, strict_paths=repo.files).findings


def test_unchanged_unbound_directive_is_not_a_global_migration(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] += "def other():\n    return call()  # noqa: S603\n"
    assert not check(repository(tmp_path, source)).findings


@pytest.mark.parametrize("enclosing", ["module", "class"])
def test_duplicate_definition_cannot_borrow_enclosing_rationale(
    tmp_path: Path, enclosing: str
) -> None:
    if enclosing == "module":
        source = files(code=["pkg/core.py:<module>"])
        source["pkg/core.py"] = (
            f"{REF}\n\n"
            "def budget():\n    return call()  # noqa: S603\n\n"
            "def budget():\n    return 1\n"
        )
    else:
        source = files(code=["pkg/core.py:Worker"])
        source["pkg/core.py"] = (
            f"{REF}\nclass Worker:\n"
            "    def budget(self):\n        return call()  # noqa: S603\n"
            "    def budget(self):\n        return 1\n"
        )
    repo = repository(tmp_path, source)
    assert any(f.code == "directive-owner" for f in check(repo, strict_paths=repo.files).findings)


@pytest.mark.parametrize("module_rationale", [False, True])
def test_module_directive_cannot_borrow_the_next_function(
    tmp_path: Path, module_rationale: bool
) -> None:
    source = files(code=["pkg/core.py:<module>" if module_rationale else "pkg/core.py:budget"])
    source["pkg/core.py"] = (
        (f"{REF}\n\n" if module_rationale else "")
        + "import external  # noqa: F401\ndef budget():\n"
        + ("" if module_rationale else f"    {REF}\n")
        + "    return 1\n"
    )
    repo = repository(tmp_path, source)
    findings = check(repo, strict_paths=repo.files).findings
    assert bool(findings) is not module_rationale
    assert all(f.code == "directive-owner" for f in findings)


def test_quiesce_marker_is_an_exact_bound_reference(tmp_path: Path) -> None:
    source = files()
    source["pkg/core.py"] = (
        "def budget():\n"
        "    # quiesce-exempt: meiva:ref pkg/docs/core.meiva.okf.md#positive-budget\n"
        "    while True:\n"
        "        sleep(1)\n"
    )
    repo = repository(tmp_path, source)
    assert not check(repo, strict_paths=repo.files).findings


@pytest.mark.parametrize("suffix", [" because it is needed", "-missing"])
def test_quiesce_reference_is_validated_even_outside_strict_scope(
    tmp_path: Path, suffix: str
) -> None:
    source = files()
    source["pkg/core.py"] = (
        f"def budget():\n    # quiesce-exempt: meiva:ref pkg/docs/core.meiva.okf.md#positive-budget{suffix}\n    while True:\n        sleep(1)\n"
    )
    assert check(repository(tmp_path, source)).findings


@pytest.mark.parametrize("path", ["pkg/core.py", "pkg/tests/test_core.py"])
@pytest.mark.parametrize("call", ["gateway_access_from_settings()", "other()"])
def test_ambient_marker_only_names_an_audited_call(tmp_path: Path, path: str, call: str) -> None:
    source = {path: f"access = {call}  # ambient: B2\n"}
    repo = repository(tmp_path, source)
    findings = check(repo, strict_paths=repo.files).findings
    assert bool(findings) is (path != "pkg/core.py" or call != "gateway_access_from_settings()")


def test_ambient_marker_cannot_include_prose(tmp_path: Path) -> None:
    repo = repository(
        tmp_path,
        {"pkg/core.py": "access = gateway_access_from_settings()  # ambient: B2 because needed\n"},
    )
    assert any(f.code == "free-comment" for f in check(repo, strict_paths=repo.files).findings)


def test_quiesce_consumer_keeps_loop_and_stale_marker_checks() -> None:
    marker = "    # quiesce-exempt: meiva:ref pkg/docs/core.meiva.okf.md#positive-budget\n"
    loop = "    while True:\n        sleep(1)\n"
    source = "def budget():\n" + marker + loop
    assert not violations_in_source(source)
    assert violations_in_source("def budget():\n" + loop)
    misplaced = "def budget():\n" + marker + "    value = 1\n" + loop
    assert any("stale" in message for _, message in violations_in_source(misplaced))
