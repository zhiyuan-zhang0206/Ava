"""Conservatively select backend tests for PR CI (enforce or shadow mode)."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_SCRIPT_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_SCRIPT_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPOSITORY_ROOT))

from base.deploy.git.repo_change import (  # noqa: E402 - direct script entry needs repo root first
    is_doc_path,
)

_FORCED_FULL_ROOTS = (
    "base/",
    "ava/",
    "agent/",
    "ava_builtins/",
    "db/",
    "migrations/",
)
_SOURCE_ROOTS = frozenset(
    {
        "agent",
        "ava",
        "cli",
        "gateway",
        "ops",
        "services",
        "base",
        "ava_builtins",
        "ui",
        "scripts",
        "schedules",
    }
)
# Where a test directory can live: the top-level `tests/` (e2e, contract and
# shared-support tests) and the `tests/` directory of any package that carries
# its own tests. A path is a test path when it sits inside such a `tests/`
# directory, so a test moving from `tests/<area>/` into `<pkg>/tests/` stays
# visible to every rule below.
_TEST_HOSTS = (
    "tests",
    "agent",
    "ava",
    "ava_builtins",
    "base",
    "cli",
    "gateway",
    "ops",
    "scripts",
    "services",
)
_QUEUE_PREFIXES = ("trunk-merge/", "trunk-temp/")
_NON_DOCUMENTATION_PREFIXES = ("scripts/", "schedules/", "tests/")
_TEST_FILE_PATTERN = re.compile(r"(?:test_.*|.*_test)\.py$")

# Tree-scan tests: their subject is the checked-out repository (the lint
# family and the repo-level CI/governance checks), so a changed source file
# can never reach them through the direct-import reverse map. They are pinned
# into every SELECTED candidate set — a green subset must not miss a tree-wide
# gate (task #4183: PR #3020's subset passed while the full population was red
# on tests/test_lint_event_kinds.py).
_TREE_SCAN_FILE_PATTERN = re.compile(r"test_lint_.*\.py$")
_TREE_SCAN_TESTS = frozenset(
    {
        "scripts/ci/tests/test_ci_job_rerun.py",
        "tests/test_ci_rerun_workflow.py",
        "scripts/ci/tests/test_ci_monitor.py",
        "tests/test_db_check_enum_sync.py",
        "tests/test_pool_keepalives.py",
        "tests/test_env_guard_canary.py",
    }
)


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
            "mode": _selection_mode(),
            "tests": list(self.tests),
            "count": self.count,
            "est_seconds": self.est_seconds,
            "full_est_seconds": self.full_est_seconds,
            "blind_changed": list(self.blind_changed),
            "forced_roots": list(self.forced_roots),
            "map_source_count": self.map_source_count,
        }


def _selection_mode() -> str:
    """The mode CI is running: the ci.yml TEST_SELECTION_MODE value.

    The selector itself behaves identically in either mode — the mode only
    decides how CI routes its decision — so this is audit metadata for the
    recorded payload, defaulting to the repository's enforce default.
    """
    return os.environ.get("TEST_SELECTION_MODE", "enforce")


def _is_test_dir_path(path: str) -> bool:
    """Whether a repo-relative path sits inside a test directory (top-level or a package's)."""
    parts = path.split("/")
    return parts[0] in _TEST_HOSTS and "tests" in parts[:-1]


def _test_py_files(repo_root: Path) -> list[Path]:
    """Every .py file inside a test directory, in a stable order."""
    files: list[Path] = []
    for host in _TEST_HOSTS:
        host_root = repo_root / host
        if host_root.is_dir():
            files.extend(
                path
                for path in host_root.rglob("*.py")
                if _is_test_dir_path(path.relative_to(repo_root).as_posix())
            )
    return sorted(files)


def collectable_test_paths(repo_root: Path) -> set[str]:
    """Return the current non-e2e backend test-file universe: every test file in a tests/ directory."""
    return {
        path.relative_to(repo_root).as_posix()
        for path in _test_py_files(repo_root)
        if _is_collectable_test_path(path.relative_to(repo_root).as_posix())
    }


def tree_scan_tests(repo_root: Path) -> set[str]:
    """Repository-wide scan tests that every SELECTED subset must include.

    Membership is the ``test_lint_*.py`` family (by name, so a new lint test
    joins automatically) plus the explicit repo-level CI/governance checks in
    ``_TREE_SCAN_TESTS``; both resolve against the collectable universe.
    """
    collectable = collectable_test_paths(repo_root)
    return {
        path
        for path in collectable
        if _TREE_SCAN_FILE_PATTERN.fullmatch(Path(path).name) is not None
        or path in _TREE_SCAN_TESTS
    }


def build_import_reverse_map(repo_root: Path) -> dict[str, set[str]]:
    """Map each statically resolved source file to its importing test files."""
    repo_root = repo_root.resolve()
    collectable = collectable_test_paths(repo_root)
    reverse_map: dict[str, set[str]] = {}

    for test_path in _test_py_files(repo_root):
        importer = test_path.relative_to(repo_root).as_posix()
        if importer not in collectable:
            continue
        for module in _imported_modules(test_path):
            source_path = _resolve_module(repo_root, module)
            if source_path is not None:
                reverse_map.setdefault(source_path, set()).add(importer)
    return reverse_map


def _early_decision(
    changed: tuple[str, ...], *, event: str, head_ref: str, full_estimate: float
) -> SelectionResult | None:
    """The rules that decide from the changed paths alone; None when the import map is needed."""
    if event != "pull_request" or head_ref.startswith(_QUEUE_PREFIXES):
        return _result("FULL", "queue-or-non-pr", full_estimate=full_estimate)
    if all(_is_documentation_path(path) for path in changed):
        return _result("SKIP", "docs-only", full_estimate=full_estimate)

    forced_roots = _forced_roots(changed)
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
    return None


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

    early = _early_decision(changed, event=event, head_ref=head_ref, full_estimate=full_estimate)
    if early is not None:
        return early

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
    # Tree-scan tests are unreachable through the direct map; pin them so a
    # SELECTED run keeps the repo-wide gates (task #4183).
    selected.update(tree_scan_tests(repo_root))
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
        print(
            f"selector mode={_selection_mode()} decision={result.decision} reason={result.reason}"
        )
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
        _is_test_dir_path(path)
        and not path.startswith("tests/e2e/")
        and Path(path).name != "conftest.py"
        and _TEST_FILE_PATTERN.fullmatch(Path(path).name) is not None
    )


def _forced_roots(changed: tuple[str, ...]) -> tuple[str, ...]:
    """The forced-full roots a change touches. A test-only edit beside the code
    (`base/x/tests/test_y.py`) is a test change, not a source change: it goes through
    the reverse map like an edit under `tests/` always did."""
    return tuple(
        root
        for root in _FORCED_FULL_ROOTS
        if any(path.startswith(root) and not _is_test_dir_path(path) for path in changed)
    )


def _is_documentation_path(path: str) -> bool:
    return (
        not path.startswith(_NON_DOCUMENTATION_PREFIXES)
        and not _is_test_dir_path(path)
        and is_doc_path(path)
    )


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
