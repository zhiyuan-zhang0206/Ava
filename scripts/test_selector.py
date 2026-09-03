"""Conservatively select backend tests for informational PR CI shadow runs."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_SCRIPT_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPT_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPOSITORY_ROOT))

from shared.repo_change import is_doc_path  # noqa: E402 - direct script entry needs repo root first

_FORCED_FULL_ROOTS = (
    "shared/",
    "ava/",
    "agent/",
    "ava_builtins/",
    "db/",
    "migrations/",
    "evals/",
)
_SOURCE_ROOTS = frozenset(
    {
        "agent",
        "ava",
        "cli",
        "gateway",
        "ops",
        "services",
        "shared",
        "ava_builtins",
        "evals",
        "ui",
        "scripts",
        "schedules",
    }
)
_QUEUE_PREFIXES = ("trunk-merge/", "trunk-temp/", "mergify/merge-queue")
_NON_DOCUMENTATION_PREFIXES = ("scripts/", "schedules/", "tests/")
_TEST_FILE_PATTERN = re.compile(r"(?:test_.*|.*_test)\.py$")


@dataclass(frozen=True)
class SelectionResult:
    """One deterministic selector decision and the data behind it."""

    decision: str
    reason: str
    tests: tuple[str, ...] = ()
    est_seconds: float = 0.0
    full_est_seconds: float = 0.0
    blind_changed: tuple[str, ...] = ()
    forced_roots: tuple[str, ...] = ()
    map_source_count: int = 0

    @property
    def count(self) -> int:
        """Return the selected test-file count."""
        return len(self.tests)

    def as_json(self) -> dict[str, object]:
        """Return the stable machine-readable selector payload."""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "mode": "shadow",
            "tests": list(self.tests),
            "count": self.count,
            "est_seconds": self.est_seconds,
            "full_est_seconds": self.full_est_seconds,
            "blind_changed": list(self.blind_changed),
            "forced_roots": list(self.forced_roots),
            "map_source_count": self.map_source_count,
        }


def collectable_test_paths(repo_root: Path) -> set[str]:
    """Return the current non-e2e backend test-file universe under tests/."""
    tests_root = repo_root / "tests"
    if not tests_root.is_dir():
        return set()
    return {
        path.relative_to(repo_root).as_posix()
        for path in tests_root.rglob("*.py")
        if _is_collectable_test_path(path.relative_to(repo_root).as_posix())
    }


def build_import_reverse_map(repo_root: Path) -> dict[str, set[str]]:
    """Map each statically resolved source file to its importing test files."""
    repo_root = repo_root.resolve()
    collectable = collectable_test_paths(repo_root)
    reverse_map: dict[str, set[str]] = {}
    tests_root = repo_root / "tests"
    if not tests_root.is_dir():
        return reverse_map

    for test_path in sorted(tests_root.rglob("*.py")):
        if test_path.name == "conftest.py":
            continue
        importer = test_path.relative_to(repo_root).as_posix()
        modules = _imported_modules(test_path)
        if importer not in collectable:
            continue
        for module in modules:
            source_path = _resolve_module(repo_root, module)
            if source_path is not None:
                reverse_map.setdefault(source_path, set()).add(importer)
    return reverse_map


def select_tests(
    changed_files: list[str],
    *,
    repo_root: Path,
    event: str = "pull_request",
    head_ref: str = "",
) -> SelectionResult:
    """Apply the ordered conservative test-selection rules to one changed-file list."""
    repo_root = repo_root.resolve()
    changed = tuple(sorted({path.strip() for path in changed_files if path.strip()}))
    collectable = collectable_test_paths(repo_root)
    durations = _load_durations(repo_root / ".test_durations")
    full_estimate = _estimate_seconds(collectable, durations)

    if event != "pull_request" or head_ref.startswith(_QUEUE_PREFIXES):
        return _result("FULL", "queue-or-non-pr", full_estimate=full_estimate)
    if all(_is_documentation_path(path) for path in changed):
        return _result("SKIP", "docs-only", full_estimate=full_estimate)

    forced_roots = tuple(
        root for root in _FORCED_FULL_ROOTS if any(path.startswith(root) for path in changed)
    )
    if forced_roots:
        return _result(
            "FULL",
            f"forced-root:{forced_roots[0]}",
            full_estimate=full_estimate,
            forced_roots=forced_roots,
        )
    if any(
        path in {"pyproject.toml", ".test_durations"}
        or path == "conftest.py"
        or path.endswith("/conftest.py")
        for path in changed
    ):
        return _result("FULL", "test-configuration", full_estimate=full_estimate)
    if any(path.startswith("tests/e2e/") for path in changed):
        return _result("FULL", "e2e", full_estimate=full_estimate)

    reverse_map = build_import_reverse_map(repo_root)
    blind_changed = tuple(
        path
        for path in changed
        if path not in collectable and path not in reverse_map and not _is_documentation_path(path)
    )
    if blind_changed:
        return _result(
            "FULL",
            "unmapped",
            full_estimate=full_estimate,
            blind_changed=blind_changed,
            map_source_count=len(reverse_map),
        )

    selected = {path for path in changed if path in collectable}
    for source_path in changed:
        selected.update(reverse_map.get(source_path, set()))
    tests = tuple(sorted(selected & collectable))
    estimate = _estimate_seconds(tests, durations, reference_paths=collectable)
    if not tests:
        return _result(
            "FULL",
            "no-tests",
            full_estimate=full_estimate,
            map_source_count=len(reverse_map),
        )
    if estimate > 0.8 * full_estimate:
        return _result(
            "FULL",
            "subset-too-close",
            est_seconds=estimate,
            full_estimate=full_estimate,
            map_source_count=len(reverse_map),
        )
    return _result(
        "SELECTED",
        "direct-imports",
        tests=tests,
        est_seconds=estimate,
        full_estimate=full_estimate,
        map_source_count=len(reverse_map),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the selector CLI and print either JSON or a concise audit summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--event", default="pull_request")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        changed_files = args.changed_files.read_text().splitlines()
    except OSError as error:
        parser.error(f"cannot read changed files: {error}")
    result = select_tests(
        changed_files,
        repo_root=args.repo_root,
        event=args.event,
        head_ref=args.head_ref,
    )
    if args.json:
        print(json.dumps(result.as_json(), sort_keys=True))
    else:
        print(f"selector mode=shadow decision={result.decision} reason={result.reason}")
        print(f"changed files={args.changed_files}")
        print(
            f"tests={result.count} estimate={result.est_seconds:.3f}s full={result.full_est_seconds:.3f}s"
        )
        print(
            f"map sources={result.map_source_count} blind={','.join(result.blind_changed) or '-'}"
        )
    return 0


def _is_collectable_test_path(path: str) -> bool:
    return (
        path.startswith("tests/")
        and not path.startswith("tests/e2e/")
        and Path(path).name != "conftest.py"
        and _TEST_FILE_PATTERN.fullmatch(Path(path).name) is not None
    )


def _is_documentation_path(path: str) -> bool:
    return not path.startswith(_NON_DOCUMENTATION_PREFIXES) and is_doc_path(path)


def _imported_modules(test_path: Path) -> set[str]:
    tree = ast.parse(test_path.read_text(), filename=str(test_path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            modules.add(node.module)
            modules.update(
                f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*"
            )
    return modules


def _resolve_module(repo_root: Path, module: str) -> str | None:
    if module.split(".", maxsplit=1)[0] not in _SOURCE_ROOTS:
        return None
    module_path = repo_root.joinpath(*module.split("."))
    source_file = module_path.with_suffix(".py")
    if source_file.is_file():
        return source_file.relative_to(repo_root).as_posix()
    package_init = module_path / "__init__.py"
    if package_init.is_file():
        return package_init.relative_to(repo_root).as_posix()
    return None


def _load_durations(path: Path) -> dict[str, float]:
    if not path.is_file():
        return {}
    data = cast(dict[object, object], json.loads(path.read_text()))
    if not isinstance(data, dict):
        raise TypeError(f"duration file must contain a JSON object: {path}")
    durations: dict[str, float] = {}
    for node_id, seconds in data.items():
        if (
            isinstance(node_id, str)
            and isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
        ):
            durations[node_id] = float(seconds)
    return durations


def _estimate_seconds(
    test_paths: set[str] | tuple[str, ...],
    durations: dict[str, float],
    *,
    reference_paths: set[str] | None = None,
) -> float:
    reference = test_paths if reference_paths is None else reference_paths
    ordered_reference = tuple(sorted(reference))
    ordered_tests = tuple(sorted(test_paths))
    reference_by_file = {
        test_path: sum(
            seconds
            for node_id, seconds in durations.items()
            if node_id.startswith(f"{test_path}::")
        )
        for test_path in ordered_reference
    }
    known_entries = [
        seconds
        for test_path in ordered_reference
        for node_id, seconds in durations.items()
        if node_id.startswith(f"{test_path}::")
    ]
    average = sum(known_entries) / len(known_entries) if known_entries else 0.0
    return sum(reference_by_file.get(test_path, 0.0) or average for test_path in ordered_tests)


def _result(
    decision: str,
    reason: str,
    *,
    tests: tuple[str, ...] = (),
    est_seconds: float = 0.0,
    full_estimate: float,
    blind_changed: tuple[str, ...] = (),
    forced_roots: tuple[str, ...] = (),
    map_source_count: int = 0,
) -> SelectionResult:
    return SelectionResult(
        decision=decision,
        reason=reason,
        tests=tests,
        est_seconds=est_seconds,
        full_est_seconds=full_estimate,
        blind_changed=blind_changed,
        forced_roots=forced_roots,
        map_source_count=map_source_count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
