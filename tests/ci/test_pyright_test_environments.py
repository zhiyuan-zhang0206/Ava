"""A package's own tests type-check with the tests standard, wherever they sit.

pyright configures by directory: an `executionEnvironments` entry names a root
directory (no globs) and the first entry, in list order, whose root contains a
file decides that file's rules. Tests used to live under `tests/<area>/`, where the
ladder holds them to the two call-signature rules at `error` and the other five
`reportUnknown*`/`reportMissing*` rules at `warning` (tests are monkeypatch-stub
code; the header of `[tool.pyright]` measures why). A test moved into
`base/**/tests/`, `ops/**/tests/` or `services/**/tests/` would fall under that
package's environment instead, which holds production code to all seven rules at
`error`; one moved into `ava/`, `scripts/` or `ava_builtins/` would fall to the
global `warning` and drop the two gated rules. Nothing reports either: pyright
just stops applying the standard. So each such directory needs its own entry,
listed above its package's, and this test says which one is missing.

Measured on 18 real test files copied into `base/`, `services/` and `ops/` test
directories (pyright on those files only): 0 errors / 41 warnings before the copy,
22 errors / 19 warnings after, every one of the 22 a warning promoted by the
package's environment.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYRIGHT = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
    "pyright"
]

_CALL_SIGNATURE_RULES = ("reportUnknownMemberType", "reportUnknownArgumentType")
_OTHER_RULES = (
    "reportUnknownVariableType",
    "reportUnknownParameterType",
    "reportMissingParameterType",
    "reportUnknownLambdaType",
    "reportMissingTypeArgument",
)
# What every test file is held to: the tests ladder rung, wherever the file sits.
TESTS_STANDARD = {
    **dict.fromkeys(_CALL_SIGNATURE_RULES, "error"),
    **dict.fromkeys(_OTHER_RULES, "warning"),
}
_HOSTS = ("agent", "ava", "ava_builtins", "base", "cli", "gateway", "ops", "scripts", "services")


def effective_rules(config: dict[str, Any], directory: str) -> dict[str, str]:
    """The seven rules pyright applies to a file in `directory` under `config`."""
    environment: dict[str, Any] = next(
        (
            env
            for env in config.get("executionEnvironments", [])
            if directory == env["root"] or directory.startswith(f"{env['root']}/")
        ),
        {},
    )
    return {rule: environment.get(rule, config[rule]) for rule in TESTS_STANDARD}


def _config(*environments: dict[str, Any]) -> dict[str, Any]:
    return {**dict.fromkeys(TESTS_STANDARD, "warning"), "executionEnvironments": list(environments)}


_STRICT_ROOT = {"root": "base", **dict.fromkeys(TESTS_STANDARD, "error")}
_TESTS_ENV = {"root": "base/packages/tests", **dict.fromkeys(_CALL_SIGNATURE_RULES, "error")}


def test_a_tests_directory_under_a_strict_package_is_held_to_the_package_rules() -> None:
    rules = effective_rules(_config(_STRICT_ROOT), "base/packages/tests")
    assert rules != TESTS_STANDARD
    assert set(rules.values()) == {"error"}


def test_its_own_entry_above_the_package_entry_restores_the_standard() -> None:
    config = _config(_TESTS_ENV, _STRICT_ROOT)
    assert effective_rules(config, "base/packages/tests") == TESTS_STANDARD
    # A subdirectory of the tests directory follows the same entry.
    assert effective_rules(config, "base/packages/tests/fixtures") == TESTS_STANDARD
    # Production code beside it keeps the package rules.
    assert set(effective_rules(config, "base/packages").values()) == {"error"}


def test_an_entry_listed_after_the_package_entry_never_applies() -> None:
    assert effective_rules(_config(_STRICT_ROOT, _TESTS_ENV), "base/packages/tests") != (
        TESTS_STANDARD
    )


def test_a_tests_directory_under_a_package_without_an_entry_falls_to_the_global_rules() -> None:
    """`ava/`, `scripts/` and `ava_builtins/` have no environment: the two gated rules are lost."""
    rules = effective_rules(_config(_STRICT_ROOT), "ava/tests")
    assert set(rules.values()) == {"warning"}


def test_a_package_environment_that_matches_the_standard_needs_no_entry() -> None:
    """`cli/`, `gateway/` and `agent/` already gate exactly the two call-signature rules."""
    config = _config({"root": "cli", **dict.fromkeys(_CALL_SIGNATURE_RULES, "error")})
    assert effective_rules(config, "cli/commands/tests") == TESTS_STANDARD


def _tracked_test_directories() -> list[str]:
    result = subprocess.run(  # noqa: S603 - fixed git query in this repository
        ["git", "-C", str(_REPO_ROOT), "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    directories = {
        path.rsplit("/", 1)[0]
        for path in result.stdout.split("\0")
        if path.split("/")[0] in _HOSTS and "tests" in path.split("/")[:-1]
    }
    return sorted(directories)


def test_every_package_tests_directory_is_held_to_the_tests_standard() -> None:
    wrong = {
        directory: effective_rules(_PYRIGHT, directory)
        for directory in _tracked_test_directories()
        if effective_rules(_PYRIGHT, directory) != TESTS_STANDARD
    }
    assert not wrong, (
        "these test directories are not type-checked to the tests standard "
        f"({dict(TESTS_STANDARD)}): {sorted(wrong)}. Add one `[[tool.pyright.executionEnvironments]]` "
        f'entry per directory (`root = "<dir>"`, reportUnknownMemberType and '
        'reportUnknownArgumentType = "error") ABOVE the entry of its package in pyproject.toml; '
        "pyright takes the first entry whose root contains the file."
    )


def test_the_real_configuration_names_all_seven_rules_globally() -> None:
    assert all(rule in _PYRIGHT for rule in TESTS_STANDARD)


@pytest.mark.parametrize("environment", _PYRIGHT["executionEnvironments"])
def test_an_environment_root_that_exists_names_a_directory(environment: dict[str, Any]) -> None:
    """A stale root (a directory that moved or was deleted) configures nothing."""
    assert (_REPO_ROOT / environment["root"]).is_dir(), environment["root"]
