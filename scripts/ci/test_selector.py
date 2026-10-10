"""Conservatively select backend tests for PR CI (enforce or shadow mode)."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

_SCRIPT_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_SCRIPT_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPOSITORY_ROOT))

from base.deploy.git.repo_change import (  # noqa: E402 - direct script entry needs repo root first
    is_doc_path,
)
from scripts.ci.test_impact import (  # noqa: E402 - standalone script
    base_checkout,
    build_impact,
    module_files,
    plugin_modules,
    unknown_diagnostics,
)
from scripts.structure.lint_common import pytest_test_hosts  # noqa: E402 - standalone script
from scripts.structure.placement import ModuleIndex  # noqa: E402 - standalone script

# Global paths apply to every test, so a change keeps the full suite. This is the
# one owner of the concept; the root conftest's `pytest_plugins` modules join it
# per checkout (`Checkout.global_files`).
_GLOBAL_FILES = frozenset(
    {
        "pyproject.toml",  # interpreter, dependencies, pytest configuration
        "uv.lock",
        "conftest.py",  # root bootstrap: `pytest_plugins` loads for every test
        ".python-version",
        ".env.example",  # the documented settings surface
    }
)
_GLOBAL_PREFIXES = (
    "tests/fixtures/",  # the suite's global fixture plugins
    "db/",
    "migrations/",
    "deploy/",  # operational configuration that tests read by path, not by import
    "commands/",  # slash-command prompt data read at runtime and by tests by path
)
# Repository-level configuration and tree-wide inputs: only the tree-scan tests
# can observe them.
_TREE_SCAN_ONLY_FILES = frozenset(
    {
        ".pre-commit-config.yaml",
        ".gitignore",
        ".gitattributes",
        ".gitleaks.toml",
        ".test_durations",  # shard balancing only
        ".test_durations.source.json",
        "LICENSE",
        "NOTICE",
    }
)
_TREE_SCAN_ONLY_PREFIXES = (
    ".github/",
    ".agents/",
    ".ava/",
    ".trunk/",
    "demos/",
    "tests/e2e/",  # CI runs the whole e2e package for every diff that is not docs-only
)
# Hidden tool directories that tests read by path rather than import. A change
# there also selects every collectable test whose source text names the directory
# (`_referencing_tests`), so the set of readers is derived, never listed by hand.
_REFERENCED_ROOTS = (".github", ".agents", ".ava", ".trunk")
# Top-level directories whose files belong to a package with an owning `tests/`.
_PACKAGE_ROOTS = frozenset(
    {
        "agent",
        "ava",
        "ava_builtins",
        "base",
        "cli",
        "gateway",
        "ops",
        "schedules",
        "scripts",
        "services",
        "tests",
        "ui",
    }
)
_QUEUE_PREFIXES = ("trunk-merge/", "trunk-temp/")
_NON_DOCUMENTATION_PREFIXES = ("schedules/", "tests/")
_TEST_FILE_PATTERN = re.compile(r"(?:test_.*|.*_test)\.py$")

# Tree-scan tests: their subject is the checked-out repository (the lint
# family and the repo-level CI/governance checks), so a changed source file
# can never reach them through the direct-import reverse map. They are pinned
# into every SELECTED candidate set — a green subset must not miss a tree-wide
# gate (task #4183: PR #3020's subset passed while the full population was red
# on tests/contracts/test_lint_event_kinds.py).
_TREE_SCAN_FILE_PATTERN = re.compile(r"test_lint_.*\.py$")
_TREE_SCAN_TESTS = frozenset(
    {
        "scripts/ci/pull_requests/tests/test_ci_job_rerun.py",
        "tests/harness/test_ci_rerun_workflow.py",
        "scripts/ci/pull_requests/tests/test_ci_monitor.py",
        "tests/contracts/test_db_check_enum_sync.py",
        "tests/harness/test_pool_keepalives.py",
        "tests/harness/test_env_guard_canary.py",
    }
)


class PathClass(StrEnum):
    """How one changed path contributes to the selection."""

    DOCUMENTATION = "documentation"  # no backend test; a docs-only diff is SKIP
    TEST = "test"  # a collectable test file and its runtime test consumers
    CONFTEST = "conftest"  # every collectable test below its directory
    PACKAGE = "package"  # runtime consumers plus the owning package's tests
    TREE_SCAN_ONLY = "tree-scan-only"  # repository-level input: tree-scan tests only
    FRONTEND = "frontend"  # ui/ non-Python: the frontend job owns it
    GLOBAL = "global"  # applies to every test: full suite
    DELETED = "deleted"  # absent from head: use base facts or explicitly keep FULL
    UNMAPPED = "unmapped"  # no rule owns it: full suite (the tracked tree has none)


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
    diagnostics: tuple[str, ...] = ()

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
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class Checkout:
    """The facts about one checked-out tree that classification needs."""

    repo_root: Path
    hosts: tuple[str, ...]
    collectable: frozenset[str]
    test_dirs: frozenset[str]  # `tests` directories that contain a collectable test
    global_files: frozenset[str]  # root-conftest plugin modules and their package inits


def _selection_mode() -> str:
    """The mode CI is running: the ci.yml TEST_SELECTION_MODE value.

    The selector itself behaves identically in either mode — the mode only
    decides how CI routes its decision — so this is audit metadata for the
    recorded payload, defaulting to the repository's enforce default.
    """
    return os.environ.get("TEST_SELECTION_MODE", "enforce")


def _is_test_dir_path(path: str, hosts: tuple[str, ...]) -> bool:
    """Whether a repo-relative path sits inside a test directory (top-level or a package's)."""
    parts = path.split("/")
    return parts[0] in hosts and "tests" in parts[:-1]


def _test_py_files(repo_root: Path) -> list[Path]:
    """Every .py file inside a test directory, in a stable order."""
    files: list[Path] = []
    hosts = pytest_test_hosts((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    for host in hosts:
        host_root = repo_root / host
        if host_root.is_dir():
            files.extend(
                path
                for path in host_root.rglob("*.py")
                if _is_test_dir_path(path.relative_to(repo_root).as_posix(), hosts)
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
    """Map runtime inputs to collectable tests through unpruned shared facts."""
    return build_impact(repo_root.resolve(), load_checkout(repo_root).collectable).tests_by_input


def load_checkout(repo_root: Path) -> Checkout:
    """Read the tree facts once so every path is classified against the same view."""
    repo_root = repo_root.resolve()
    hosts = pytest_test_hosts((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    collectable = frozenset(collectable_test_paths(repo_root))
    return Checkout(
        repo_root=repo_root,
        hosts=hosts,
        collectable=collectable,
        test_dirs=frozenset(d for path in collectable for d in _tests_directories(path)),
        global_files=frozenset(_plugin_files(repo_root)),
    )


def _tests_directories(path: str) -> list[str]:
    """Every ancestor directory of ``path`` that is named ``tests``."""
    parts = path.split("/")
    return [
        "/".join(parts[: index + 1]) for index, part in enumerate(parts[:-1]) if part == "tests"
    ]


def classify_path(path: str, checkout: Checkout) -> PathClass:
    """The single place that decides how a changed repo-relative path is handled.

    ``select_tests`` and the tracked-tree completeness test both call this, so
    the rules exist once. The first matching rule wins.
    """
    if _is_documentation_path(path, checkout.hosts):
        return PathClass.DOCUMENTATION
    if Path(path).name == "conftest.py":
        # Checked before existence: a deleted conftest still changes the fixtures
        # of every test below it, with no import to break.
        return PathClass.GLOBAL if path == "conftest.py" else PathClass.CONFTEST
    if not os.path.lexists(checkout.repo_root / path):
        return PathClass.DELETED
    if path in checkout.collectable:
        return PathClass.TEST
    return _repository_class(path, checkout)


def _repository_class(path: str, checkout: Checkout) -> PathClass:
    """The class of an existing, non-test, non-conftest path."""
    if path in _GLOBAL_FILES or path in checkout.global_files or path.startswith(_GLOBAL_PREFIXES):
        return PathClass.GLOBAL
    if path in _TREE_SCAN_ONLY_FILES or path.startswith(_TREE_SCAN_ONLY_PREFIXES):
        return PathClass.TREE_SCAN_ONLY
    root = path.split("/", maxsplit=1)[0]
    if root == "ui" and not path.endswith(".py"):
        return PathClass.FRONTEND
    if root in _PACKAGE_ROOTS and "/" in path:
        return PathClass.PACKAGE
    return PathClass.UNMAPPED


def package_tests(path: str, checkout: Checkout) -> set[str]:
    """Collectable tests of the package that owns ``path``.

    Walk up from the path's directory; the nearest ``tests`` directory that holds
    a collectable test (the directory itself when the path is inside one, else a
    sibling ``tests`` beside an ancestor) owns the path, and every collectable test
    below it is the package's test set.
    """
    directory = path.rpartition("/")[0]
    while True:
        tests_dir = (
            directory
            if directory.rpartition("/")[2] == "tests"
            else f"{directory}/tests".lstrip("/")
        )
        if tests_dir in checkout.test_dirs:
            return {test for test in checkout.collectable if test.startswith(f"{tests_dir}/")}
        if not directory:
            return set()
        directory = directory.rpartition("/")[0]


def _conftest_tests(path: str, checkout: Checkout) -> set[str]:
    """The collectable tests a conftest.py can affect: its directory subtree."""
    prefix = path.rpartition("/")[0] + "/"
    return {test for test in checkout.collectable if test.startswith(prefix)}


def _reference_pattern(root: str) -> re.Pattern[str]:
    """A string-literal reference to a top-level directory: ``.github/`` or ``".github"``.

    The unquoted form must not follow a word character or a dot, so the module
    name ``base.agents`` does not reference ``.agents``.
    """
    escaped = re.escape(root)
    return re.compile(rf"(?<![\w.]){escaped}/|[\"']{escaped}[\"']")


def _referencing_tests(root: str, checkout: Checkout) -> set[str]:
    """Collectable tests whose source text names the top-level directory ``root``."""
    pattern = _reference_pattern(root)
    return {
        test
        for test in checkout.collectable
        if pattern.search((checkout.repo_root / test).read_text(encoding="utf-8"))
    }


def _path_tests(
    path: str, path_class: PathClass, checkout: Checkout, reverse_map: dict[str, set[str]]
) -> set[str]:
    """What one changed path contributes to the candidate subset."""
    runtime: set[str] = set()
    for parent in (Path(path), *Path(path).parents):
        runtime.update(reverse_map.get(parent.as_posix(), set()))
    if path_class is PathClass.TEST:
        return {path} | runtime
    if path_class is PathClass.CONFTEST:
        return _conftest_tests(path, checkout) | runtime
    if path_class is PathClass.PACKAGE:
        return runtime | package_tests(path, checkout)
    root = path.split("/", maxsplit=1)[0]
    if path_class is PathClass.TREE_SCAN_ONLY and root in _REFERENCED_ROOTS:
        return _referencing_tests(root, checkout) | runtime
    return runtime


def _owner_tests(
    changed: dict[str, PathClass], checkout: Checkout, reverse_map: dict[str, set[str]]
) -> set[str]:
    """The union of what each changed path contributes (rules by class)."""
    selected: set[str] = set()
    for path, path_class in changed.items():
        selected.update(_path_tests(path, path_class, checkout, reverse_map))
    return selected


def select_tests(
    changed_files: list[str],
    *,
    repo_root: Path,
    event: str = "pull_request",
    head_ref: str = "",
    base_ref: str | None = None,
) -> SelectionResult:
    """Apply the ordered conservative test-selection rules to one changed-file list."""
    repo_root = repo_root.resolve()
    changed = tuple(sorted({path.strip() for path in changed_files if path.strip()}))
    checkout = load_checkout(repo_root)
    durations = _load_durations(repo_root / ".test_durations")
    full_estimate = _estimate_seconds(checkout.collectable, durations)

    if event != "pull_request" or head_ref.startswith(_QUEUE_PREFIXES):
        return _result("FULL", "queue-or-non-pr", full_estimate=full_estimate)
    if all(_is_documentation_path(path, checkout.hosts) for path in changed):
        return _result("SKIP", "docs-only", full_estimate=full_estimate)

    classes = {path: classify_path(path, checkout) for path in changed}
    forced = _forced_full(classes, full_estimate)
    if forced is not None:
        return forced

    candidates = _runtime_candidates(classes, checkout, base_ref, full_estimate)
    if isinstance(candidates, SelectionResult):
        return candidates
    selected, source_count = candidates
    # Tree-scan tests are unreachable through the owner rules; pin them so a
    # SELECTED run keeps the repo-wide gates (task #4183).
    selected.update(tree_scan_tests(repo_root))
    tests = tuple(sorted(selected & checkout.collectable))
    estimate = _estimate_seconds(tests, durations, reference_paths=set(checkout.collectable))
    if not tests:
        return _result(
            "FULL",
            "no-tests",
            full_estimate=full_estimate,
            map_source_count=source_count,
        )
    if estimate > 0.8 * full_estimate:
        return _result(
            "FULL",
            "subset-too-close",
            est_seconds=estimate,
            full_estimate=full_estimate,
            map_source_count=source_count,
        )
    return _result(
        "SELECTED",
        "owner-tests",
        tests=tests,
        est_seconds=estimate,
        full_estimate=full_estimate,
        map_source_count=source_count,
    )


def _runtime_candidates(
    classes: dict[str, PathClass],
    checkout: Checkout,
    base_ref: str | None,
    full_estimate: float,
) -> tuple[set[str], int] | SelectionResult:
    """Union both trees' runtime impact, declining a subset when evidence is incomplete."""
    impact = build_impact(checkout.repo_root, checkout.collectable)
    reverse_map = impact.tests_by_input
    diagnostics = list(unknown_diagnostics(impact, tree="head"))
    selected = _owner_tests(classes, checkout, reverse_map)
    if base_ref is not None:
        with base_checkout(checkout.repo_root, base_ref) as base_root:
            base = load_checkout(base_root)
            old_impact = build_impact(base_root, base.collectable & checkout.collectable)
            diagnostics.extend(unknown_diagnostics(old_impact, tree="base"))
            old_classes = {path: classify_path(path, base) for path in classes}
            old_forced = _forced_full(old_classes, full_estimate)
            if old_forced is not None:
                return old_forced
            selected.update(_owner_tests(old_classes, base, old_impact.tests_by_input))
            for path, tests_for_path in old_impact.tests_by_input.items():
                reverse_map.setdefault(path, set()).update(tests_for_path & checkout.collectable)
    elif any(kind is PathClass.DELETED for kind in classes.values()):
        diagnostics.extend(
            f"head:{path}: deleted path requires --base-ref to recover runtime impact"
            for path, kind in classes.items()
            if kind is PathClass.DELETED
        )
    if diagnostics:
        return _result(
            "FULL",
            "incomplete-impact",
            full_estimate=full_estimate,
            map_source_count=len(reverse_map),
            diagnostics=tuple(sorted(set(diagnostics))),
        )
    return selected, len(reverse_map)


def _forced_full(classes: dict[str, PathClass], full_estimate: float) -> SelectionResult | None:
    """FULL for a global path or, as the runtime safety net, an unowned path."""
    global_paths = tuple(path for path, kind in classes.items() if kind is PathClass.GLOBAL)
    if global_paths:
        return _result(
            "FULL",
            f"global-path:{global_paths[0]}",
            full_estimate=full_estimate,
            forced_roots=global_paths,
        )
    unmapped = tuple(path for path, kind in classes.items() if kind is PathClass.UNMAPPED)
    if unmapped:
        return _result("FULL", "unmapped", full_estimate=full_estimate, blind_changed=unmapped)
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the selector CLI and print either JSON or a concise audit summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--event", default="pull_request")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--base-ref", help="Committed merge-base for removed dependencies and paths"
    )
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
        base_ref=args.base_ref,
    )
    for diagnostic in result.diagnostics:
        print(f"test-impact: {diagnostic}", file=sys.stderr)
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
        not path.startswith("tests/e2e/")
        and Path(path).name != "conftest.py"
        and _TEST_FILE_PATTERN.fullmatch(Path(path).name) is not None
    )


def _is_documentation_path(path: str, hosts: tuple[str, ...]) -> bool:
    return (
        not path.startswith(_NON_DOCUMENTATION_PREFIXES)
        and not _is_test_dir_path(path, hosts)
        and is_doc_path(path)
    )


def _plugin_modules(repo_root: Path) -> list[str]:
    """The dotted modules the root conftest lists in ``pytest_plugins``."""
    conftest = repo_root / "conftest.py"
    if not conftest.is_file():
        return []
    return list(plugin_modules(ast.parse(conftest.read_text(), filename=str(conftest))))


def _plugin_files(repo_root: Path) -> set[str]:
    """Repo files every pytest process loads: plugin modules and their package inits.

    A module that does not resolve to a repo file (an installed plugin such as
    ``pytester``) has no path to change here and is skipped.
    """
    index = ModuleIndex(repo_root)
    return {file for module in _plugin_modules(repo_root) for file in module_files(module, index)}


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
    test_paths: set[str] | frozenset[str] | tuple[str, ...],
    durations: dict[str, float],
    *,
    reference_paths: set[str] | None = None,
) -> float:
    reference = test_paths if reference_paths is None else reference_paths
    ordered_reference = tuple(sorted(reference))
    ordered_tests = tuple(sorted(test_paths))
    reference_set = set(reference)
    # Group once: the complete timing model has an entry for every measured
    # node, so scanning it again for each file makes selection unnecessarily slow.
    by_file: dict[str, list[float]] = {}
    for node_id, seconds in durations.items():
        test_path, separator, _ = node_id.partition("::")
        if separator and test_path in reference_set:
            by_file.setdefault(test_path, []).append(seconds)
    known_entries = [
        seconds for test_path in ordered_reference for seconds in by_file.get(test_path, ())
    ]
    average = sum(known_entries) / len(known_entries) if known_entries else 0.0
    return sum(
        sum(by_file[test_path]) if test_path in by_file else average for test_path in ordered_tests
    )


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
    diagnostics: tuple[str, ...] = (),
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
        diagnostics=diagnostics,
    )


if __name__ == "__main__":
    raise SystemExit(main())
