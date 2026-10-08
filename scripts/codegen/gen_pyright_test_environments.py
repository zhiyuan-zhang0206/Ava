#!/usr/bin/env python
"""Generate the pyright `executionEnvironments` entries for package-level tests directories.

A package's own tests live in `<pkg>/**/tests/`. pyright configures by directory: an
`executionEnvironments` entry names a root (a plain directory, no globs) and the FIRST
entry, in list order, whose root contains a file decides that file's rules. A tests
directory under `base/`, `ops/` or `services/` would inherit the package's seven `error`
rules; one under `ava/`, `scripts/` or `ava_builtins/` would fall to the global `warning`
and lose the two gated call-signature rules. Either way pyright just stops applying the
tests standard, silently. So each such directory needs its own entry, listed above the
entry of its package, and hand-writing one per directory (about 150 are planned) turns
into hundreds of lines that every test-moving change edits in the same place.

This script owns that list. It reads test hosts from pytest `testpaths`, scans their
tracked `<pkg>/**/tests/` directories and writes
one entry per directory that is not already at the tests standard into the region of
`pyproject.toml` fenced by the BEGIN/END comments below. Everything outside the region is
hand-written and left byte-for-byte alone.

* The region is sorted by root and each entry is its own five lines, blank line included,
  so two changes adding entries only collide when their roots are neighbors in that order.
  Either resolves by running this script again.
* The entry sets exactly the two call-signature rules to `error`; the other five rules
  come from the global configuration (`warning`).
* A tests directory inside a hand-written entry that carries more than rules (the
  `extraPaths` of `ava_builtins/skills/integrations/web-ai`) repeats those settings, because the
  generated entry takes precedence and would otherwise drop them.

Run after adding, moving or removing a tests directory, or let the pre-commit
`lint-pyright-test-environments` hook fail loud:

    .venv/bin/python scripts/codegen/gen_pyright_test_environments.py

`--check` exits 1 without writing when the region is stale.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure.lint_common import pytest_test_hosts  # noqa: E402 - standalone script

BEGIN = (
    "# BEGIN GENERATED pyright tests environments "
    "(scripts/codegen/gen_pyright_test_environments.py; edit nothing between the markers)"
)
END = "# END GENERATED pyright tests environments"

CALL_SIGNATURE_RULES = ("reportUnknownMemberType", "reportUnknownArgumentType")
OTHER_RULES = (
    "reportUnknownVariableType",
    "reportUnknownParameterType",
    "reportMissingParameterType",
    "reportUnknownLambdaType",
    "reportMissingTypeArgument",
)
# What every test file is held to: the tests ladder rung, wherever the file sits.
TESTS_STANDARD = {
    **dict.fromkeys(CALL_SIGNATURE_RULES, "error"),
    **dict.fromkeys(OTHER_RULES, "warning"),
}


def _covers(root: str, directory: str) -> bool:
    return directory == root or directory.startswith(f"{root}/")


def _covering_environment(config: dict[str, Any], directory: str) -> dict[str, Any]:
    """The first `executionEnvironments` entry whose root contains `directory`, else {}."""
    environments: list[dict[str, Any]] = config.get("executionEnvironments", [])
    none: dict[str, Any] = {}
    return next((env for env in environments if _covers(env["root"], directory)), none)


def effective_rules(config: dict[str, Any], directory: str) -> dict[str, str]:
    """The seven rules pyright applies to a file in `directory` under `config`."""
    environment = _covering_environment(config, directory)
    return {rule: environment.get(rule, config[rule]) for rule in TESTS_STANDARD}


def tests_roots(tracked: Iterable[str], hosts: tuple[str, ...]) -> list[str]:
    """The outermost `tests/` directory of every tracked module in a package, sorted."""
    roots: set[str] = set()
    for path in tracked:
        parts = path.split("/")
        if parts[0] != "tests" and parts[0] in hosts and "tests" in parts[:-1]:
            roots.add("/".join(parts[: parts.index("tests") + 1]))
    return sorted(roots)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        items = cast("list[object]", value)
        if all(isinstance(item, str) for item in items):
            return json.dumps(items)
    raise SystemExit(
        f"cannot repeat a {type(value).__name__} setting in a generated entry: {value!r}"
    )


def _entries(config: dict[str, Any], roots: list[str]) -> list[dict[str, Any]]:
    """One entry per root that the hand-written `config` does not already hold to the standard."""
    entries: list[dict[str, Any]] = []
    for root in roots:
        if effective_rules(config, root) == TESTS_STANDARD:
            continue
        inherited = {
            key: value
            for key, value in _covering_environment(config, root).items()
            if key != "root" and key not in TESTS_STANDARD
        }
        entries.append({"root": root, **inherited, **dict.fromkeys(CALL_SIGNATURE_RULES, "error")})
    return entries


def _render(entries: list[dict[str, Any]]) -> str:
    blocks = [
        "[[tool.pyright.executionEnvironments]]\n"
        + "".join(f"{key} = {_toml_value(value)}\n" for key, value in entry.items())
        + "\n"
        for entry in entries
    ]
    return f"{BEGIN}\n{''.join(blocks)}{END}\n"


def _region(text: str) -> re.Match[str]:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(
            "pyproject.toml must contain exactly one generated region: a line "
            f"{BEGIN!r} followed by a line {END!r}"
        )
    match = re.search(rf"^{re.escape(BEGIN)}\n.*?^{re.escape(END)}\n", text, re.S | re.M)
    if match is None:
        raise SystemExit("the generated region markers of pyproject.toml are out of order")
    return match


def generate(pyproject_text: str, tracked: Iterable[str]) -> str:
    """`pyproject_text` with its generated region rewritten for the tracked tests directories."""
    region = _region(pyproject_text)
    handwritten = pyproject_text[: region.start()] + pyproject_text[region.end() :]
    config = tomllib.loads(handwritten)["tool"]["pyright"]
    rendered = _render(_entries(config, tests_roots(tracked, pytest_test_hosts(handwritten))))
    return pyproject_text[: region.start()] + rendered + pyproject_text[region.end() :]


def _tracked_python_files(repo_root: Path) -> list[str]:
    result = subprocess.run(  # noqa: S603 - fixed git query in a repository
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def main(argv: list[str] | None = None, *, repo_root: Path = _REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if pyproject.toml is stale; write nothing"
    )
    args = parser.parse_args(argv)
    pyproject = repo_root / "pyproject.toml"
    current = pyproject.read_text(encoding="utf-8")
    fresh = generate(current, _tracked_python_files(repo_root))
    if fresh == current:
        return 0
    if args.check:
        print(
            "pyproject.toml: the pyright tests environments do not match the tests directories.\n"
            "Run .venv/bin/python scripts/codegen/gen_pyright_test_environments.py to regenerate.",
            file=sys.stderr,
        )
        return 1
    pyproject.write_text(fresh, encoding="utf-8")
    print("pyproject.toml: pyright tests environments regenerated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
