"""Contract tests for the conservative PR backend test selector.

Each test runs the selector against a miniature checkout so import discovery,
changed-file handling, and timing estimates remain independent of this repo's
current source tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from time import perf_counter

import pytest

from scripts import test_selector

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_queue_branch_always_keeps_the_full_suite(tmp_path: Path) -> None:
    """A queue branch must never let a selected subset replace its full net."""
    result = test_selector.select_tests(
        ["cli/commands.py"],
        repo_root=tmp_path,
        event="pull_request",
        head_ref="trunk-merge/batch-42",
    )

    assert result.decision == "FULL"
    assert result.reason == "queue-or-non-pr"

    push_result = test_selector.select_tests(["cli/commands.py"], repo_root=tmp_path, event="push")

    assert push_result.decision == "FULL"
    assert push_result.reason == "queue-or-non-pr"


def _write(repo_root: Path, relative_path: str, content: str = "") -> None:
    path = repo_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _selector_repo(tmp_path: Path) -> Path:
    """Build a small checkout with direct imports and known test timings."""
    files = {
        "agent/exec_child.py": "",
        "cli/commands.py": "",
        "ops/worker.py": "",
        "scripts/only_helper.py": "",
        "shared/lm/__init__.py": "",
        "tests/unit/conftest.py": "import cli.commands\n",
        "tests/unit/helper.py": "from scripts import only_helper\n",
        "tests/unit/test_changed.py": "def test_changed(): pass\n",
        "tests/unit/test_imports.py": (
            "import cli.commands\n"
            "from ops import worker\n"
            "\n"
            "def test_imports(): pass\n"
            "\n"
            "def lazy_imports():\n"
            "    from agent import exec_child\n"
            "    from shared import lm\n"
        ),
        "tests/unit/test_other.py": "def test_other(): pass\n",
        "tests/e2e/test_browser.py": "import cli.commands\n",
    }
    for relative_path, content in files.items():
        _write(tmp_path, relative_path, content)
    _write(
        tmp_path,
        ".test_durations",
        json.dumps(
            {
                "tests/e2e/test_browser.py::test_browser": 100.0,
                "tests/unit/test_changed.py::test_changed": 2.0,
                "tests/unit/test_imports.py::test_imports": 3.0,
                "tests/unit/test_other.py::test_other": 5.0,
            }
        ),
    )
    return tmp_path


def test_builds_a_direct_import_reverse_map_from_every_test_scope(tmp_path: Path) -> None:
    """Removing a lazy import must remove its importer from the source map."""
    repo_root = _selector_repo(tmp_path)

    reverse_map = test_selector.build_import_reverse_map(repo_root)

    expected_importer = {"tests/unit/test_imports.py"}
    assert reverse_map["agent/exec_child.py"] == expected_importer
    assert reverse_map["cli/commands.py"] == expected_importer
    assert reverse_map["ops/worker.py"] == expected_importer
    assert reverse_map["shared/lm/__init__.py"] == expected_importer
    assert "scripts/only_helper.py" not in reverse_map


def test_selects_mapped_sources_and_changed_tests_in_sorted_order(tmp_path: Path) -> None:
    """A reversed diff must still run its direct tests plus its new test file."""
    repo_root = _selector_repo(tmp_path)

    result = test_selector.select_tests(
        ["tests/unit/test_changed.py", "cli/commands.py"], repo_root=repo_root
    )

    assert result.decision == "SELECTED"
    assert result.reason == "direct-imports"
    assert result.tests == ("tests/unit/test_changed.py", "tests/unit/test_imports.py")
    assert result.count == 2
    assert result.est_seconds == 5.0
    assert result.full_est_seconds == 10.0


@pytest.mark.parametrize(
    ("changed_path", "reason"),
    [
        ("shared/repo_change.py", "forced-root:shared/"),
        ("pyproject.toml", "test-configuration"),
        (".test_durations", "test-configuration"),
        ("tests/e2e/test_browser.py", "e2e"),
        ("ops/unmapped.py", "unmapped"),
    ],
)
def test_forces_full_for_unsafe_paths(tmp_path: Path, changed_path: str, reason: str) -> None:
    """Removing a conservative escape hatch must leave an unsafe subset behind."""
    result = test_selector.select_tests([changed_path], repo_root=_selector_repo(tmp_path))

    assert result.decision == "FULL"
    assert result.reason == reason


def test_docs_only_skips_but_schedule_docs_stay_conservative(tmp_path: Path) -> None:
    """A schedule document is operational input, not a harmless docs-only change."""
    repo_root = _selector_repo(tmp_path)

    docs = test_selector.select_tests(["conventions/guide.md"], repo_root=repo_root)
    schedule_doc = test_selector.select_tests(["schedules/guide.md"], repo_root=repo_root)

    assert docs.decision == "SKIP"
    assert schedule_doc.decision == "FULL"
    assert schedule_doc.reason == "unmapped"


def test_duration_guard_falls_back_to_full_when_subset_is_nearly_full(tmp_path: Path) -> None:
    """Raising the selected duration past 80% must decline the subset run."""
    repo_root = _selector_repo(tmp_path)
    _write(
        repo_root,
        ".test_durations",
        json.dumps(
            {
                "tests/unit/test_changed.py::test_changed": 0.5,
                "tests/unit/test_imports.py::test_imports": 9.0,
                "tests/unit/test_other.py::test_other": 0.5,
            }
        ),
    )

    result = test_selector.select_tests(["cli/commands.py"], repo_root=repo_root)

    assert result.decision == "FULL"
    assert result.reason == "subset-too-close"
    assert result.est_seconds == 9.0
    assert result.full_est_seconds == 10.0


def test_uses_average_duration_for_a_new_test_without_a_timing_entry(tmp_path: Path) -> None:
    """A missing timing must cost the known average instead of being free."""
    repo_root = _selector_repo(tmp_path)
    _write(repo_root, "tests/unit/test_new.py", "def test_new(): pass\n")

    result = test_selector.select_tests(["tests/unit/test_new.py"], repo_root=repo_root)

    assert result.decision == "SELECTED"
    assert result.tests == ("tests/unit/test_new.py",)
    assert abs(result.est_seconds - 10 / 3) < 1e-9
    assert abs(result.full_est_seconds - 40 / 3) < 1e-9


def test_uses_the_average_timing_entry_for_an_unmeasured_test_file(tmp_path: Path) -> None:
    """A multi-test known file must not inflate the cost of one new file."""
    repo_root = _selector_repo(tmp_path)
    _write(repo_root, "tests/unit/test_new.py", "def test_new(): pass\n")
    _write(
        repo_root,
        ".test_durations",
        json.dumps(
            {
                "tests/unit/test_changed.py::test_changed": 2.0,
                "tests/unit/test_imports.py::test_fast": 1.0,
                "tests/unit/test_imports.py::test_slow": 2.0,
                "tests/unit/test_other.py::test_other": 5.0,
            }
        ),
    )

    result = test_selector.select_tests(["tests/unit/test_new.py"], repo_root=repo_root)

    assert result.est_seconds == 2.5
    assert result.full_est_seconds == 12.5


def test_json_cli_output_is_machine_readable_and_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Changing input order must not make the CI audit trail unstable."""
    repo_root = _selector_repo(tmp_path)
    changed_files = repo_root / "changed.txt"
    changed_files.write_text("tests/unit/test_changed.py\ncli/commands.py\n")

    assert (
        test_selector.main(
            [
                "--changed-files",
                str(changed_files),
                "--repo-root",
                str(repo_root),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision"] == "SELECTED"
    assert payload["mode"] == "shadow"
    assert payload["tests"] == ["tests/unit/test_changed.py", "tests/unit/test_imports.py"]
    assert payload["map_source_count"] == 4

    repeated = test_selector.select_tests(
        ["cli/commands.py", "tests/unit/test_changed.py"], repo_root=repo_root
    )
    assert repeated.as_json() == payload


def test_duration_estimates_are_identical_across_python_hash_seeds() -> None:
    """Hash-randomized set iteration must not change shadow audit JSON."""
    snippet = """\
from scripts.test_selector import _estimate_seconds

print(repr(_estimate_seconds(
    {\"tests/a.py\", \"tests/b.py\", \"tests/c.py\"},
    {
        \"tests/a.py::test_a\": 1e16,
        \"tests/b.py::test_b\": 1.0,
        \"tests/c.py::test_c\": 1.0,
    },
)))
"""
    outputs: list[str] = []
    for seed in ("1", "2", "3"):
        environment = os.environ | {"PYTHONHASHSEED": seed}
        completed = subprocess.run(  # noqa: S603 -- fixed local Python snippet
            [sys.executable, "-c", snippet],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=True,
            env=environment,
            text=True,
        )
        out = completed.stdout or ""
        outputs.append(out)

    assert len(set(outputs)) == 1


def test_real_repository_map_is_fast_and_has_broad_static_coverage() -> None:
    """The production checkout must keep the selector map usable in CI."""
    started = perf_counter()

    result = test_selector.select_tests(["scripts/test_selector.py"], repo_root=_REPO_ROOT)

    assert result.map_source_count > 500
    assert result.full_est_seconds > 0
    assert perf_counter() - started < 60


def test_cli_reports_an_unreadable_changed_file_as_an_invocation_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An absent changed-file input must use argparse's documented exit code."""
    with pytest.raises(SystemExit) as error:
        test_selector.main(["--changed-files", str(tmp_path / "missing.txt")])

    assert error.value.code == 2
    assert "cannot read changed files" in capsys.readouterr().err
