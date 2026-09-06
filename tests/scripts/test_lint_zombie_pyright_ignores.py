"""Contract tests for the zombie Pyright-ignore registry gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import lint_zombie_pyright_ignores as gate


@pytest.fixture
def tier_config(tmp_path: Path) -> gate.TierConfig:
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        """
[tool.pyright]
typeCheckingMode = "strict"
reportUnknownMemberType = "warning"
reportUnknownArgumentType = "warning"
reportUnknownVariableType = "warning"
reportUnknownParameterType = "warning"
reportMissingParameterType = "warning"
reportUnknownLambdaType = "warning"
reportMissingTypeArgument = "warning"
reportMissingTypeStubs = false
reportPrivateUsage = "none"

[[tool.pyright.executionEnvironments]]
root = "shared"
reportUnknownMemberType = "error"
reportUnknownArgumentType = "error"
reportPrivateUsage = "error"

[[tool.pyright.executionEnvironments]]
root = "shared/lenient"
reportUnknownArgumentType = "error"
""".lstrip(),
        encoding="utf-8",
    )
    return gate.load_tier_config(config_path)


def _write(repo: Path, rel_path: str, content: str) -> Path:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _ignore(path: str, line: int, rule: str) -> gate.PyrightIgnore:
    return gate.PyrightIgnore(path=path, line=line, rule=rule)


def test_tier_config_uses_longest_environment_root(tier_config: gate.TierConfig) -> None:
    assert tier_config.level_for("shared/model.py", "reportUnknownMemberType") == "error"
    assert tier_config.level_for("shared/lenient/model.py", "reportUnknownMemberType") == "warning"
    assert tier_config.level_for("shared/lenient/model.py", "reportUnknownArgumentType") == "error"
    assert tier_config.level_for("sharedness/model.py", "reportUnknownMemberType") == "warning"
    assert tier_config.level_for("agent/model.py", "reportUnknownMemberType") == "warning"


def test_scan_finds_rules_only_in_python_comment_tokens(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "agent/example.py",
        """
value = 1  # pyright: ignore[reportUnknownMemberType, reportAssignmentType]
    # pyright: ignore[reportUnknownArgumentType]
literal = "# pyright: ignore[reportUnknownVariableType]"
doc = '''# pyright: ignore[reportUnknownLambdaType]'''
""".lstrip(),
    )

    assert gate.scan_file(path, "agent/example.py") == [
        _ignore("agent/example.py", 1, "reportUnknownMemberType"),
        _ignore("agent/example.py", 1, "reportAssignmentType"),
        _ignore("agent/example.py", 2, "reportUnknownArgumentType"),
    ]


def test_repository_scan_accepts_an_injected_file_list(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "agent/a.py",
        "value = 1  # pyright: ignore[reportUnknownMemberType]\n",
    )
    _write(
        tmp_path,
        "agent/unselected.py",
        "value = 1  # pyright: ignore[reportUnknownArgumentType]\n",
    )

    assert gate.scan_repository(files=["agent/a.py"], repo_root=tmp_path) == [
        _ignore("agent/a.py", 1, "reportUnknownMemberType")
    ]


def test_classify_keeps_error_tier_and_non_family_rules(
    tier_config: gate.TierConfig,
) -> None:
    ignores = [
        _ignore("shared/model.py", 1, "reportUnknownMemberType"),
        _ignore("agent/model.py", 2, "reportUnknownMemberType"),
        _ignore("agent/model.py", 3, "reportAssignmentType"),
        _ignore("agent/model.py", 4, "reportPrivateUsage"),
        _ignore("agent/model.py", 5, "reportMissingTypeStubs"),
        _ignore("agent/model.py", 6, "reportUnusedParameter"),
        _ignore("shared/model.py", 7, "reportPrivateUsage"),
        _ignore("agent/model.py", 8, "reportMadeUpRule"),
    ]

    scan = gate.classify_ignores(ignores, tier_config)

    assert scan.zombies == frozenset({ignores[1], ignores[3], ignores[4], ignores[5]})
    assert scan.invalid == frozenset({ignores[7]})


@pytest.mark.parametrize(
    ("registry_entries", "zombies", "expected_stderr"),
    [
        ([], [_ignore("agent/a.py", 3, "reportUnknownMemberType")], "new zombie"),
        (
            [_ignore("agent/a.py", 3, "reportUnknownMemberType")],
            [],
            "no longer corresponds",
        ),
    ],
)
def test_check_rejects_new_and_stale_registry_entries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    registry_entries: list[gate.PyrightIgnore],
    zombies: list[gate.PyrightIgnore],
    expected_stderr: str,
) -> None:
    registry = tmp_path / "registry"
    gate.write_registry(registry, frozenset(registry_entries))

    assert gate.check_registry(gate.ScanResult(frozenset(zombies), frozenset()), registry) == 1
    captured = capsys.readouterr()
    assert "agent/a.py:3:reportUnknownMemberType" in captured.out
    assert expected_stderr in captured.err


def test_check_accepts_matching_empty_sets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = tmp_path / "registry"
    gate.write_registry(registry, frozenset())

    assert gate.check_registry(gate.ScanResult(frozenset(), frozenset()), registry) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "0 zombie" in captured.err


def test_check_rejects_invalid_rule_without_registry_membership(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = _ignore("agent/a.py", 7, "reportMadeUpRule")
    registry = tmp_path / "registry"
    gate.write_registry(registry, frozenset())

    assert gate.check_registry(gate.ScanResult(frozenset(), frozenset({invalid})), registry) == 1
    captured = capsys.readouterr()
    assert f"invalid: {invalid.registry_entry}" in captured.out
    assert "new invalid Pyright rule" in captured.err


def test_check_accepts_grandfathered_invalid_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    invalid = _ignore("agent/a.py", 7, "reportMadeUpRule")
    zombie = _ignore("agent/a.py", 8, "reportUnknownMemberType")
    registry = tmp_path / "registry"
    gate.write_registry(registry, frozenset({invalid, zombie}))

    assert (
        gate.check_registry(gate.ScanResult(frozenset({zombie}), frozenset({invalid})), registry)
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""


def test_docstring_lookalike_is_not_scanned() -> None:
    path = Path(__file__).parents[2] / "tests/agent/hook_typing_contract.py"
    ignores = gate.scan_file(path, "tests/agent/hook_typing_contract.py")
    assert not any(ignore.line == 13 for ignore in ignores)


@pytest.mark.parametrize(
    ("source", "line", "expected"),
    [
        (
            "# pyright: ignore[reportUnknownMemberType]\nkept = 1\n",
            1,
            "kept = 1\n",
        ),
        (
            "kept = value  # pyright: ignore[reportUnknownMemberType] reason\n",
            1,
            "kept = value\n",
        ),
    ],
)
def test_strip_single_rule_removes_comment_but_preserves_code(
    tmp_path: Path, source: str, line: int, expected: str
) -> None:
    path = _write(tmp_path, "agent/a.py", source)
    candidate = _ignore("agent/a.py", line, "reportUnknownMemberType")

    gate.strip_file(path, frozenset({candidate}))

    assert path.read_text(encoding="utf-8") == expected


def test_strip_multiple_rules_removes_only_selected_rule(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "agent/a.py",
        "value = other  # pyright: ignore[reportUnknownMemberType, reportAssignmentType]\n",
    )
    candidate = _ignore("agent/a.py", 1, "reportUnknownMemberType")

    gate.strip_file(path, frozenset({candidate}))

    assert path.read_text(encoding="utf-8") == (
        "value = other  # pyright: ignore[reportAssignmentType]\n"
    )


def test_verify_multiline_call_rejects_load_bearing_rule_at_call_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        tmp_path,
        "agent/a.py",
        "monkeypatch.setattr(\n"
        "    target,\n"
        "    'attribute',\n"
        "    lambda value: value,\n"
        ")  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]\n",
    )
    candidates = frozenset(gate.scan_file(path, "agent/a.py"))

    def fake_pyright_errors(candidate_path: Path) -> frozenset[tuple[int, str]]:
        errors = {(9, "reportAssignmentType")}
        if "reportUnknownArgumentType" not in candidate_path.read_text(encoding="utf-8"):
            errors.add((1, "reportUnknownArgumentType"))
        return frozenset(errors)

    monkeypatch.setattr(gate, "_pyright_errors", fake_pyright_errors)

    verification = gate.verify_file(path, candidates)
    gate.strip_file(path, verification.certified)

    assert verification.rejected == frozenset(
        {_ignore("agent/a.py", 5, "reportUnknownArgumentType")}
    )
    assert verification.certified == frozenset(
        {_ignore("agent/a.py", 5, "reportUnknownLambdaType")}
    )
    assert path.read_text(encoding="utf-8").endswith(
        ")  # pyright: ignore[reportUnknownArgumentType]\n"
    )


def test_verify_single_line_still_rejects_load_bearing_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        tmp_path,
        "agent/a.py",
        "call(value)  # pyright: ignore[reportUnknownArgumentType]\n",
    )
    candidate = _ignore("agent/a.py", 1, "reportUnknownArgumentType")

    def fake_pyright_errors(candidate_path: Path) -> frozenset[tuple[int, str]]:
        if "reportUnknownArgumentType" in candidate_path.read_text(encoding="utf-8"):
            return frozenset()
        return frozenset({(1, "reportUnknownArgumentType")})

    monkeypatch.setattr(gate, "_pyright_errors", fake_pyright_errors)

    verification = gate.verify_file(path, frozenset({candidate}))

    assert verification.certified == frozenset()
    assert verification.rejected == frozenset({candidate})


def test_verify_multiline_call_strips_all_warning_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        tmp_path,
        "agent/a.py",
        "call(\n"
        "    lambda value: value,\n"
        ")  # pyright: ignore[reportUnknownLambdaType, reportUnusedCallResult]\n",
    )
    candidates = frozenset(gate.scan_file(path, "agent/a.py"))

    def no_pyright_errors(_path: Path) -> frozenset[tuple[int, str]]:
        return frozenset()

    monkeypatch.setattr(gate, "_pyright_errors", no_pyright_errors)

    verification = gate.verify_file(path, candidates)
    gate.strip_file(path, verification.certified)

    assert verification.certified == candidates
    assert verification.rejected == frozenset()
    assert path.read_text(encoding="utf-8") == "call(\n    lambda value: value,\n)\n"
