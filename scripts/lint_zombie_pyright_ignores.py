"""Keep Pyright-ignore comments aligned with the repository's severity ladder.

An ignore is a zombie whenever its effective diagnostic severity is not an
error: it hides no merge-blocking finding and misstates the local type contract.
This definition rests on the CI contract that only error-severity Pyright
diagnostics gate merge (``uv run pyright`` and the pre-commit hook both exit 0
on warnings — see the [tool.pyright] ladder comment in pyproject.toml); if
warning-severity diagnostics ever become blocking, this classification must
be revisited. Effective severity combines strict-mode defaults with the
global and execution-environment overrides in ``pyproject.toml``. A
bidirectional registry allows the existing debt while rejecting both new
zombies and stale entries after a zombie is removed.

Run ``--freeze`` only to establish a reviewed baseline. ``--check`` is the
default CI path. ``--verify`` removes ignores in a temporary sibling and asks
Pyright whether any errors appear beyond the original baseline. ``--strip`` applies
only those certified removals; ``--strip --all-tier`` skips Pyright and removes
the statically known warning-tier family rules.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tempfile
import tokenize
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_REGISTRY_PATH = _REPO_ROOT / "scripts/zombie_pyright_ignores.registry"
_PYRIGHT_PATH = _REPO_ROOT / ".venv/bin/pyright"

_FAMILY_RULES = frozenset(
    {
        "reportMissingParameterType",
        "reportMissingTypeArgument",
        "reportUnknownArgumentType",
        "reportUnknownLambdaType",
        "reportUnknownMemberType",
        "reportUnknownParameterType",
        "reportUnknownVariableType",
    }
)

# Pyright diagnostic-rule catalog and the tagged unused-parameter diagnostic
# named explicitly by this repository's cleanup contract:
# https://github.com/microsoft/pyright/blob/main/docs/configuration.md#diagnostic-rule-defaults
_ALL_PYRIGHT_RULES = frozenset(
    {
        "reportAbstractUsage",
        "reportArgumentType",
        "reportAssertAlwaysTrue",
        "reportAssertTypeFailure",
        "reportAssignmentType",
        "reportAttributeAccessIssue",
        "reportCallInDefaultInitializer",
        "reportCallIssue",
        "reportConstantRedefinition",
        "reportDeprecated",
        "reportDuplicateImport",
        "reportFunctionMemberAccess",
        "reportGeneralTypeIssues",
        "reportImplicitOverride",
        "reportImplicitStringConcatenation",
        "reportImportCycles",
        "reportIncompatibleMethodOverride",
        "reportIncompatibleVariableOverride",
        "reportIncompleteStub",
        "reportInconsistentConstructor",
        "reportInconsistentOverload",
        "reportIndexIssue",
        "reportInvalidStringEscapeSequence",
        "reportInvalidStubStatement",
        "reportInvalidTypeArguments",
        "reportInvalidTypeForm",
        "reportInvalidTypeVarUse",
        "reportMatchNotExhaustive",
        "reportMissingImports",
        "reportMissingModuleSource",
        "reportMissingParameterType",
        "reportMissingSuperCall",
        "reportMissingTypeArgument",
        "reportMissingTypeStubs",
        "reportNoOverloadImplementation",
        "reportOperatorIssue",
        "reportOptionalCall",
        "reportOptionalContextManager",
        "reportOptionalIterable",
        "reportOptionalMemberAccess",
        "reportOptionalOperand",
        "reportOptionalSubscript",
        "reportOverlappingOverload",
        "reportPossiblyUnboundVariable",
        "reportPrivateImportUsage",
        "reportPrivateUsage",
        "reportPropertyTypeMismatch",
        "reportRedeclaration",
        "reportReturnType",
        "reportSelfClsParameterName",
        "reportTypeCommentUsage",
        "reportTypedDictNotRequiredAccess",
        "reportUnboundVariable",
        "reportUndefinedVariable",
        "reportUnhashable",
        "reportUninitializedInstanceVariable",
        "reportUnknownArgumentType",
        "reportUnknownLambdaType",
        "reportUnknownMemberType",
        "reportUnknownParameterType",
        "reportUnknownVariableType",
        "reportUnnecessaryCast",
        "reportUnnecessaryComparison",
        "reportUnnecessaryContains",
        "reportUnnecessaryIsInstance",
        "reportUnnecessaryTypeIgnoreComment",
        "reportUnreachable",
        "reportUnsupportedDunderAll",
        "reportUntypedBaseClass",
        "reportUntypedClassDecorator",
        "reportUntypedFunctionDecorator",
        "reportUntypedNamedTuple",
        "reportUnusedCallResult",
        "reportUnusedClass",
        "reportUnusedCoroutine",
        "reportUnusedExcept",
        "reportUnusedExpression",
        "reportUnusedFunction",
        "reportUnusedImport",
        "reportUnusedParameter",
        "reportUnusedVariable",
        "reportWildcardImportFromLibrary",
    }
)

# Rules whose default severity is "error" in Pyright strict mode. Rules absent
# here default to a non-blocking severity unless pyproject.toml overrides them.
# Source: the Strict column in the diagnostic-settings table linked above.
_STRICT_ENABLED_RULES = frozenset(
    {
        "reportAbstractUsage",
        "reportArgumentType",
        "reportAssertAlwaysTrue",
        "reportAssertTypeFailure",
        "reportAssignmentType",
        "reportAttributeAccessIssue",
        "reportCallIssue",
        "reportConstantRedefinition",
        "reportDeprecated",
        "reportDuplicateImport",
        "reportFunctionMemberAccess",
        "reportGeneralTypeIssues",
        "reportIncompatibleMethodOverride",
        "reportIncompatibleVariableOverride",
        "reportIncompleteStub",
        "reportInconsistentConstructor",
        "reportInconsistentOverload",
        "reportIndexIssue",
        "reportInvalidStringEscapeSequence",
        "reportInvalidStubStatement",
        "reportInvalidTypeArguments",
        "reportInvalidTypeForm",
        "reportInvalidTypeVarUse",
        "reportMatchNotExhaustive",
        "reportMissingImports",
        "reportMissingParameterType",
        "reportMissingTypeArgument",
        "reportMissingTypeStubs",
        "reportNoOverloadImplementation",
        "reportOperatorIssue",
        "reportOptionalCall",
        "reportOptionalContextManager",
        "reportOptionalIterable",
        "reportOptionalMemberAccess",
        "reportOptionalOperand",
        "reportOptionalSubscript",
        "reportOverlappingOverload",
        "reportPossiblyUnboundVariable",
        "reportPrivateImportUsage",
        "reportPrivateUsage",
        "reportRedeclaration",
        "reportReturnType",
        "reportSelfClsParameterName",
        "reportTypeCommentUsage",
        "reportTypedDictNotRequiredAccess",
        "reportUnboundVariable",
        "reportUndefinedVariable",
        "reportUnhashable",
        "reportUnknownArgumentType",
        "reportUnknownLambdaType",
        "reportUnknownMemberType",
        "reportUnknownParameterType",
        "reportUnknownVariableType",
        "reportUnnecessaryCast",
        "reportUnnecessaryComparison",
        "reportUnnecessaryContains",
        "reportUnnecessaryIsInstance",
        "reportUntypedBaseClass",
        "reportUntypedClassDecorator",
        "reportUntypedFunctionDecorator",
        "reportUntypedNamedTuple",
        "reportUnsupportedDunderAll",
        "reportUnusedClass",
        "reportUnusedCoroutine",
        "reportUnusedExcept",
        "reportUnusedExpression",
        "reportUnusedFunction",
        "reportUnusedImport",
        "reportUnusedVariable",
        "reportWildcardImportFromLibrary",
    }
)

if not _FAMILY_RULES <= _ALL_PYRIGHT_RULES:
    raise ValueError("family rules must be present in the Pyright rule catalog")
if not _STRICT_ENABLED_RULES <= _ALL_PYRIGHT_RULES:
    raise ValueError("strict rules must be present in the Pyright rule catalog")

_IGNORE_RE = re.compile(r"#\s*pyright:\s*ignore\[([^\]]+)\]")
_NON_BLOCKING_LEVELS = frozenset({"warning", "none", "information"})

DiagnosticLevel = Literal["error", "warning", "information", "none"]


@dataclass(frozen=True, order=True)
class PyrightIgnore:
    path: str
    line: int
    rule: str

    @property
    def registry_entry(self) -> str:
        return f"{self.path}:{self.line}:{self.rule}"


@dataclass(frozen=True)
class ScanResult:
    zombies: frozenset[PyrightIgnore]
    invalid: frozenset[PyrightIgnore]


@dataclass(frozen=True)
class Verification:
    certified: frozenset[PyrightIgnore]
    rejected: frozenset[PyrightIgnore]


@dataclass(frozen=True)
class TierConfig:
    global_levels: Mapping[str, DiagnosticLevel]
    environment_levels: Mapping[str, Mapping[str, DiagnosticLevel]]

    @property
    def error_tier_roots(self) -> Mapping[str, frozenset[str]]:
        return {
            root: frozenset(rule for rule, level in levels.items() if level == "error")
            for root, levels in self.environment_levels.items()
        }

    def level_for(self, path: str, rule: str) -> DiagnosticLevel:
        matching_roots = [
            root for root in self.environment_levels if _path_is_within_root(path, root)
        ]
        if matching_roots:
            root = max(matching_roots, key=lambda value: len(PurePosixPath(value).parts))
            environment = self.environment_levels[root]
            if rule in environment:
                return environment[rule]
        if rule in self.global_levels:
            return self.global_levels[rule]
        return "error" if rule in _STRICT_ENABLED_RULES else "none"


def _as_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a TOML table")
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise ValueError(f"{label} must be a TOML table")
    return cast(dict[str, object], mapping)


def _diagnostic_level(value: object, label: str) -> DiagnosticLevel:
    if value is True:
        return "error"
    if value is False:
        return "none"
    if value not in {"error", "warning", "information", "none"}:
        raise ValueError(f"{label} has unknown diagnostic level {value!r}")
    return cast(DiagnosticLevel, value)


def load_tier_config(path: Path) -> TierConfig:
    document = _as_mapping(tomllib.loads(path.read_text(encoding="utf-8")), str(path))
    tool = _as_mapping(document["tool"], "tool")
    pyright = _as_mapping(tool["pyright"], "tool.pyright")
    if pyright["typeCheckingMode"] != "strict":
        raise ValueError("tool.pyright.typeCheckingMode must be strict")
    global_levels: dict[str, DiagnosticLevel] = {
        rule: _diagnostic_level(pyright[rule], f"tool.pyright.{rule}")
        for rule in _ALL_PYRIGHT_RULES
        if rule in pyright
    }

    environments: dict[str, dict[str, DiagnosticLevel]] = {}
    raw_environments = pyright["executionEnvironments"]
    if not isinstance(raw_environments, list):
        raise TypeError("tool.pyright.executionEnvironments must be an array of tables")
    for index, raw_environment in enumerate(cast(list[object], raw_environments)):
        environment = _as_mapping(raw_environment, f"tool.pyright.executionEnvironments[{index}]")
        root_value = environment["root"]
        if not isinstance(root_value, str):
            raise TypeError(f"tool.pyright.executionEnvironments[{index}].root must be a string")
        root = PurePosixPath(root_value.strip("/")).as_posix()
        if root in environments:
            raise ValueError(f"duplicate Pyright execution-environment root: {root}")
        environments[root] = {
            rule: _diagnostic_level(
                environment[rule],
                f"tool.pyright.executionEnvironments[{index}].{rule}",
            )
            for rule in _ALL_PYRIGHT_RULES
            if rule in environment
        }
    return TierConfig(global_levels, environments)


def _path_is_within_root(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


_TIER_CONFIG = load_tier_config(_PYPROJECT_PATH)
_ERROR_TIER_ROOTS = _TIER_CONFIG.error_tier_roots


def scan_file(path: Path, relative_path: str) -> list[PyrightIgnore]:
    source = path.read_text(encoding="utf-8")
    ignores: list[PyrightIgnore] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        for match in _IGNORE_RE.finditer(token.string):
            for raw_rule in match.group(1).split(","):
                ignores.append(PyrightIgnore(relative_path, token.start[0], raw_rule.strip()))
    return ignores


def _tracked_python_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def _relative_file(path: str | Path, repo_root: Path) -> tuple[Path, str]:
    candidate = Path(path)
    absolute = candidate if candidate.is_absolute() else repo_root / candidate
    relative = absolute.resolve().relative_to(repo_root.resolve()).as_posix()
    return absolute, relative


def scan_repository(
    *,
    files: Sequence[str | Path] | None = None,
    repo_root: Path = _REPO_ROOT,
) -> list[PyrightIgnore]:
    selected = list(files) if files is not None else _tracked_python_files(repo_root)
    ignores: list[PyrightIgnore] = []
    for selected_path in selected:
        absolute, relative = _relative_file(selected_path, repo_root)
        ignores.extend(scan_file(absolute, relative))
    return ignores


def classify_ignores(ignores: Iterable[PyrightIgnore], config: TierConfig) -> ScanResult:
    zombies: set[PyrightIgnore] = set()
    invalid: set[PyrightIgnore] = set()
    for ignore in ignores:
        if ignore.rule not in _ALL_PYRIGHT_RULES:
            invalid.add(ignore)
        elif config.level_for(ignore.path, ignore.rule) in _NON_BLOCKING_LEVELS:
            zombies.add(ignore)
    return ScanResult(frozenset(zombies), frozenset(invalid))


def read_registry(path: Path) -> frozenset[PyrightIgnore]:
    entries: set[PyrightIgnore] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parts = raw_line.rsplit(":", 2)
        if len(parts) != 3 or not parts[0] or not parts[1].isdigit() or not parts[2]:
            raise ValueError(f"invalid zombie-ignore registry entry: {raw_line!r}")
        entry = PyrightIgnore(parts[0], int(parts[1]), parts[2])
        if entry in entries:
            raise ValueError(f"duplicate zombie-ignore registry entry: {raw_line}")
        entries.add(entry)
    return frozenset(entries)


def write_registry(path: Path, entries: frozenset[PyrightIgnore]) -> None:
    content = "".join(f"{entry.registry_entry}\n" for entry in sorted(entries))
    path.write_text(content, encoding="utf-8")


def _print_entries(entries: Iterable[PyrightIgnore], label: str | None = None) -> None:
    for entry in sorted(entries):
        print(f"{label}: {entry.registry_entry}" if label else entry.registry_entry)


def check_registry(scan: ScanResult, registry_path: Path) -> int:
    registry = read_registry(registry_path)
    current = scan.zombies | scan.invalid
    new_entries = current - registry
    stale_entries = registry - current
    _print_entries((entry for entry in new_entries if entry in scan.invalid), "invalid")
    _print_entries((entry for entry in new_entries if entry in scan.zombies), "zombie")
    _print_entries(stale_entries, "stale")

    if new_entries & scan.invalid:
        print(
            f"{len(new_entries & scan.invalid)} new invalid Pyright rule entr"
            f"{'y' if len(new_entries & scan.invalid) == 1 else 'ies'} found.",
            file=sys.stderr,
        )
    new_zombies = new_entries & scan.zombies
    if new_zombies:
        print(
            f"{len(new_zombies)} new zombie Pyright ignore(s): delete them or raise "
            "the rule back to the error tier.",
            file=sys.stderr,
        )
    if stale_entries:
        print(
            f"{len(stale_entries)} registry entr"
            f"{'y' if len(stale_entries) == 1 else 'ies'} no longer corresponds to a "
            "zombie; remove cleared ignores from the registry too.",
            file=sys.stderr,
        )
    print(
        f"{len(scan.zombies)} zombie ignore(s), {len(scan.invalid)} invalid rule(s), "
        f"{len(registry)} registry entry(ies).",
        file=sys.stderr,
    )
    return int(bool(new_entries or stale_entries))


def _comment_matches(source: str) -> dict[int, tuple[int, re.Match[str]]]:
    matches: dict[int, tuple[int, re.Match[str]]] = {}
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        match = _IGNORE_RE.search(token.string)
        if match is not None:
            matches[token.start[0]] = (token.start[1], match)
    return matches


def strip_file(
    path: Path,
    candidates: frozenset[PyrightIgnore],
    *,
    preserve_line_numbers: bool = False,
) -> None:
    if not candidates:
        return
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    comments = _comment_matches(source)
    candidates_by_line: dict[int, set[str]] = {}
    for candidate in candidates:
        candidates_by_line.setdefault(candidate.line, set()).add(candidate.rule)

    removed: set[tuple[int, str]] = set()
    for line_number, rules_to_remove in candidates_by_line.items():
        if line_number not in comments:
            continue
        comment_column, match = comments[line_number]
        rules = [rule.strip() for rule in match.group(1).split(",")]
        remaining = [rule for rule in rules if rule not in rules_to_remove]
        removed.update((line_number, rule) for rule in rules if rule in rules_to_remove)
        line = lines[line_number - 1]
        if remaining:
            start = comment_column + match.start(1)
            end = comment_column + match.end(1)
            lines[line_number - 1] = line[:start] + ", ".join(remaining) + line[end:]
            continue

        comment_start = comment_column + match.start()
        prefix = line[:comment_start].rstrip(" \t")
        if prefix:
            newline = "\n" if line.endswith("\n") else ""
            lines[line_number - 1] = prefix + newline
        else:
            lines[line_number - 1] = "\n" if preserve_line_numbers and line.endswith("\n") else ""

    expected = {(candidate.line, candidate.rule) for candidate in candidates}
    if removed != expected:
        missing = sorted(expected - removed)
        raise ValueError(f"strip candidates no longer match file contents: {missing!r}")
    path.write_text("".join(lines), encoding="utf-8")


def _pyright_errors(path: Path) -> frozenset[tuple[int, str]]:
    result = subprocess.run(  # noqa: S603 - fixed repo-local executable, no shell
        [str(_PYRIGHT_PATH), "--outputjson", str(path)],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        payload = _as_mapping(json.loads(result.stdout), "Pyright JSON output")
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Pyright did not return valid JSON for {path}: {result.stderr.strip()}"
        ) from exc
    diagnostics = payload["generalDiagnostics"]
    if not isinstance(diagnostics, list):
        raise TypeError("Pyright generalDiagnostics must be an array")

    errors: set[tuple[int, str]] = set()
    for raw_diagnostic in cast(list[object], diagnostics):
        diagnostic = _as_mapping(raw_diagnostic, "Pyright diagnostic")
        if diagnostic["severity"] != "error" or "rule" not in diagnostic:
            continue
        rule = diagnostic["rule"]
        if not isinstance(rule, str):
            raise TypeError("Pyright diagnostic rule must be a string")
        diagnostic_range = _as_mapping(diagnostic["range"], "Pyright diagnostic range")
        start = _as_mapping(diagnostic_range["start"], "Pyright diagnostic range start")
        line = start["line"]
        if not isinstance(line, int):
            raise TypeError("Pyright diagnostic line must be an integer")
        errors.add((line + 1, rule))
    return frozenset(errors)


def verify_file(path: Path, candidates: frozenset[PyrightIgnore]) -> Verification:
    baseline_errors = _pyright_errors(path)
    source = path.read_text(encoding="utf-8")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.stem}.zombie-ignore-",
        suffix=".py",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    rejected: set[PyrightIgnore] = set()
    try:
        for candidate in sorted(candidates):
            temporary_path.write_text(source, encoding="utf-8")
            strip_file(
                temporary_path,
                frozenset({candidate}),
                preserve_line_numbers=True,
            )
            if _pyright_errors(temporary_path) - baseline_errors:
                rejected.add(candidate)
    finally:
        temporary_path.unlink(missing_ok=True)

    rejected_candidates = frozenset(rejected)
    return Verification(candidates - rejected_candidates, rejected_candidates)


def _verification_json(results: Mapping[str, Verification]) -> str:
    payload = {
        path: {
            "certified": [item.registry_entry for item in sorted(result.certified)],
            "rejected": [item.registry_entry for item in sorted(result.rejected)],
        }
        for path, result in sorted(results.items())
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _selected_ignores(files: Sequence[str]) -> dict[str, tuple[Path, list[PyrightIgnore]]]:
    selected: dict[str, tuple[Path, list[PyrightIgnore]]] = {}
    for file_arg in files:
        absolute, relative = _relative_file(file_arg, _REPO_ROOT)
        selected[relative] = (absolute, scan_file(absolute, relative))
    return selected


def _verify_selected(files: Sequence[str]) -> tuple[dict[str, Verification], ScanResult]:
    selected = _selected_ignores(files)
    all_ignores = [ignore for _, ignores in selected.values() for ignore in ignores]
    scan = classify_ignores(all_ignores, _TIER_CONFIG)
    results: dict[str, Verification] = {}
    for relative, (path, ignores) in selected.items():
        candidates = frozenset(ignore for ignore in ignores if ignore.rule in _ALL_PYRIGHT_RULES)
        results[relative] = verify_file(path, candidates)
    return results, scan


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--freeze", action="store_true", help="replace the registry")
    modes.add_argument("--check", action="store_true", help="check the registry (default)")
    modes.add_argument(
        "--verify",
        nargs="+",
        metavar="FILE",
        help="verify removable ignores (warning-severity release is tolerated; only new errors reject)",
    )
    modes.add_argument("--strip", nargs="+", metavar="FILE", help="remove certified ignores")
    parser.add_argument(
        "--all-tier",
        action="store_true",
        help="with --strip, remove all statically known non-error ignores",
    )
    parser.add_argument("--registry", type=Path, default=_REGISTRY_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.all_tier and args.strip is None:
        _build_parser().error("--all-tier requires --strip")

    if args.verify is not None:
        verification, scan = _verify_selected(cast(list[str], args.verify))
        _print_entries(scan.invalid, "invalid")
        print(_verification_json(verification))
        return 0

    if args.strip is not None:
        files = cast(list[str], args.strip)
        selected = _selected_ignores(files)
        if args.all_tier:
            all_ignores = [ignore for _, ignores in selected.values() for ignore in ignores]
            scan = classify_ignores(all_ignores, _TIER_CONFIG)
            _print_entries(scan.invalid, "invalid")
            certified_by_path = {
                relative: frozenset(scan.zombies.intersection(ignores))
                for relative, (_, ignores) in selected.items()
            }
        else:
            verification, scan = _verify_selected(files)
            _print_entries(scan.invalid, "invalid")
            certified_by_path = {
                relative: result.certified for relative, result in verification.items()
            }
        for relative, (path, _) in selected.items():
            strip_file(path, certified_by_path[relative])
            _print_entries(certified_by_path[relative])
        print(
            f"Stripped {sum(len(items) for items in certified_by_path.values())} "
            "certified ignore rule(s).",
            file=sys.stderr,
        )
        return 0

    scan = classify_ignores(scan_repository(), _TIER_CONFIG)
    if args.freeze:
        entries = scan.zombies | scan.invalid
        _print_entries(scan.invalid, "invalid")
        write_registry(args.registry, entries)
        print(
            f"Wrote {len(entries)} zombie/invalid ignore(s) to {args.registry} "
            f"({len(scan.zombies)} zombies, {len(scan.invalid)} invalid rules).",
            file=sys.stderr,
        )
        return 0
    return check_registry(scan, args.registry)


if __name__ == "__main__":
    sys.exit(main())
