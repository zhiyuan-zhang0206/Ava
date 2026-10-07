"""Sweeper tool coverage for scripts/structure/cochange.py: per-commit package
spread (Metric A) and cross-package co-change pairs (Metric B) over a
synthetic git repo, plus one smoke test against this repo's real history.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import subprocess
from typing import Any

import pytest

from scripts.structure import cochange


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 — fixed test commands, never external input
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Cochange test",
            "-c",
            "user.email=cochange-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: pathlib.Path) -> None:
    # Pin the branch: the tool reads `main` by default, and a CI runner's git
    # defaults the initial branch to `master`.
    _git(root, "init", "--quiet", "--initial-branch=main")


def _commit(root: pathlib.Path, message: str, files: dict[str, str]) -> None:
    """Write/overwrite each relative path with its content and commit them all."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", message)


def _run(root: pathlib.Path, *extra: str) -> Any:
    """Run the tool against `root` and return the parsed --json report."""
    exit_code, output = _run_capture(root, "--json", *extra)
    assert exit_code == 0
    return json.loads(output)


def _run_capture(root: pathlib.Path | None, *extra: str) -> tuple[int, str]:
    """Run `cochange.main` and capture stdout, without going through a subprocess.

    Defaults the window to a generous `--commits` so a synthetic repo's few
    commits are never trimmed by the tool's own 90-day default; a caller that
    passes its own `--days`/`--commits` keeps that instead (argparse takes
    the later occurrence).
    """
    window = [] if any(flag in extra for flag in ("--days", "--commits")) else ["--commits", "1000"]
    # A synthetic repo has no remote: read its local `main` (the tool defaults to origin/main).
    branch = [] if root is None or "--branch" in extra else ["--branch", "main"]
    args = [*(["--repo", str(root)] if root is not None else []), *window, *branch, *extra]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cochange.main(args)
    return code, buf.getvalue()


# --- pure helpers -------------------------------------------------------


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("gateway/routers/foo.py", True),
        ("tests/gateway/foo.py", False),  # tests/ directory
        ("gateway/test_foo.py", False),  # test_ prefix
        ("ui/web/src/components/Foo.test.tsx", False),  # *.test.*
        ("ui/web/src/__tests__/foo.tsx", False),  # __tests__/
        ("docs/foo.md", False),  # not a src extension
        (
            "scripts/structure/baseline/agent.graph.json",
            False,
        ),  # generated (glob + not src ext anyway)
        ("ui/web/openapi.json", False),  # generated
        ("ui/web/src/lib/api-generated.ts", False),  # generated glob
        ("db/schema.sql", False),  # generated
        ("ui/web/src/lib/transport/api.ts", True),
        ("migrations/20260101T000000_x.sql", True),
        ("scripts/install-cli-tools.sh", True),
    ],
)
def test_is_src_classification(rel_path: str, expected: bool) -> None:
    assert cochange._is_src(rel_path) is expected


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("gateway/routers/foo.py", "gateway/routers"),
        ("scripts/lint_x.py", "scripts"),
        ("ui/web/src/components/Foo.tsx", "ui/web/src/components"),
        ("ui/web/src/foo.ts", "ui/web/src"),
        ("ui/web/src/components/forms/Foo.tsx", "ui/web/src/components"),
        ("migrations/x.sql", "migrations"),
    ],
)
def test_package_of_file(rel_path: str, expected: str) -> None:
    assert cochange._package_of_file(rel_path) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("old.py => new.py", ("old.py", "new.py")),
        ("dir/{old => new}/file.py", ("dir/old/file.py", "dir/new/file.py")),
        ("pkg_a/{mod.py => renamed.py}", ("pkg_a/mod.py", "pkg_a/renamed.py")),
        ("gateway/routers/foo.py", (None, "gateway/routers/foo.py")),
    ],
)
def test_numstat_paths_split_a_rename(raw: str, expected: tuple[str | None, str]) -> None:
    assert cochange._numstat_paths(raw) == expected


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("fix(gateway): do a thing", "fix"),
        ("[Ava-123] fix(gateway): do a thing", "fix"),
        ("feat(cli)!: breaking change", "feat"),
        ("refactor(shared): tidy", "refactor"),
        ("chore: bump deps", "other"),
        ("no conventional prefix here", "other"),
    ],
)
def test_commit_type_bucketing(subject: str, expected: str) -> None:
    assert cochange._commit_type(subject) == expected


def test_percentile_nearest_rank() -> None:
    assert cochange._percentile([1, 2, 3], 0.5) == 2
    assert cochange._percentile([1, 2, 3], 0.9) == 3
    assert cochange._percentile([], 0.5) == 0


# --- Metric A: spread + percentiles by type, and the fix >= 3 share -----


def test_spread_percentiles_and_fix_wide_share(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "chore: seed", {"pkg_a/__init__.py": "x = 1\n"})
    # fix #1: spread 1 (touches only pkg_a)
    _commit(tmp_path, "fix(a): one package", {"pkg_a/mod.py": "x = 1\n"})
    # fix #2: spread 2 (pkg_a, pkg_b)
    _commit(
        tmp_path,
        "fix(ab): two packages",
        {"pkg_a/mod.py": "x = 2\n", "pkg_b/mod.py": "x = 1\n"},
    )
    # fix #3: spread 3 (pkg_a, pkg_b, pkg_c) — the only one with spread >= 3
    _commit(
        tmp_path,
        "fix(abc): three packages",
        {"pkg_a/mod.py": "x = 3\n", "pkg_b/mod.py": "x = 2\n", "pkg_c/mod.py": "x = 1\n"},
    )

    report = _run(tmp_path)

    fix_stats = report["spread_by_type"]["fix"]
    assert fix_stats == {"n": 3, "p50": 2, "p90": 3}
    assert report["fix_count"] == 3
    assert report["fix_wide_count"] == 1
    assert report["fix_wide_share"] == pytest.approx(1 / 3)
    widest = report["widest_fix"]
    assert widest[0]["subject"] == "fix(abc): three packages"
    assert widest[0]["spread"] == 3
    assert widest[0]["packages"] == ["pkg_a", "pkg_b", "pkg_c"]


# --- Metric B: strong pairs, same-package exclusion, contract boundary --


def test_strong_cross_package_pair_is_detected(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    seed = {"pkg_a/__init__.py": "x = 1\n", "pkg_b/__init__.py": "x = 1\n"}
    _commit(tmp_path, "chore: seed", seed)
    for i in range(8):
        _commit(
            tmp_path,
            f"fix(ab): touch both {i}",
            {"pkg_a/mod.py": f"x = {i}\n", "pkg_b/mod.py": f"x = {i}\n"},
        )

    report = _run(tmp_path, "--min-support", "8", "--min-confidence", "0.6")

    pairs = {(row["a"], row["b"]) for row in report["strong_pairs"]}
    assert ("pkg_a/mod.py", "pkg_b/mod.py") in pairs
    wanted = ("pkg_a/mod.py", "pkg_b/mod.py")
    row = next(r for r in report["strong_pairs"] if (r["a"], r["b"]) == wanted)
    assert row["c"] == 8
    assert row["n_a"] == 8
    assert row["n_b"] == 8
    assert row["confidence"] == pytest.approx(1.0)
    package_pairs = {(row["a"], row["b"]) for row in report["package_pairs"]}
    assert ("pkg_a", "pkg_b") in package_pairs


def test_same_package_pair_is_never_reported(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "chore: seed", {"pkg_a/__init__.py": "x = 1\n"})
    for i in range(8):
        _commit(
            tmp_path,
            f"fix(a): touch both files in one package {i}",
            {"pkg_a/mod.py": f"x = {i}\n", "pkg_a/other.py": f"y = {i}\n"},
        )

    report = _run(tmp_path, "--min-support", "1", "--min-confidence", "0.0")

    pairs = {(row["a"], row["b"]) for row in report["strong_pairs"]}
    assert ("pkg_a/mod.py", "pkg_a/other.py") not in pairs
    assert not any(a.startswith("pkg_a/") and b.startswith("pkg_a/") for a, b in pairs)


def test_contract_boundary_pair_is_skipped(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    _commit(
        tmp_path,
        "chore: seed",
        {"gateway/schemas/api.py": "x = 1\n", "ui/web/src/lib/transport/api.ts": "x = 1\n"},
    )
    for i in range(8):
        _commit(
            tmp_path,
            f"feat(contract): sync {i}",
            {
                "gateway/schemas/api.py": f"x = {i}\n",
                "ui/web/src/lib/transport/api.ts": f"x = {i}\n",
            },
        )
    # A real, non-boundary cross-package pair, so the filter isn't a no-op.
    for i in range(8):
        _commit(
            tmp_path,
            f"fix(control): unrelated pair {i}",
            {"pkg_a/mod.py": f"x = {i}\n", "pkg_b/mod.py": f"x = {i}\n"},
        )

    report = _run(tmp_path, "--min-support", "8", "--min-confidence", "0.6")

    pairs = {(row["a"], row["b"]) for row in report["strong_pairs"]}
    assert not any(
        "gateway/schemas/" in (a, b) or "ui/web/" in a or "ui/web/" in b
        for a, b in pairs
        if a.startswith("gateway/schemas/") or b.startswith("gateway/schemas/")
    )
    assert ("gateway/schemas/api.py", "ui/web/src/lib/transport/api.ts") not in pairs
    assert ("pkg_a/mod.py", "pkg_b/mod.py") in pairs  # the control pair still shows up


def test_bulk_commit_is_excluded_from_pairing(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    bulk_files = {f"pkg_a/bulk_{i}.py": "x = 1\n" for i in range(21)}
    bulk_files.update({f"pkg_b/bulk_{i}.py": "x = 1\n" for i in range(20)})
    _commit(tmp_path, "chore: bulk drop (41 files)", bulk_files)  # > 40 src files
    for i in range(2):
        _commit(
            tmp_path,
            f"fix(ab): small pair {i}",
            {"pkg_a/mod.py": f"x = {i}\n", "pkg_b/mod.py": f"x = {i}\n"},
        )

    report = _run(tmp_path, "--min-support", "2", "--min-confidence", "0.5")

    assert report["paired_commit_count"] == 2  # the 41-file commit is excluded
    pairs = {(row["a"], row["b"]) for row in report["strong_pairs"]}
    assert pairs == {("pkg_a/mod.py", "pkg_b/mod.py")}
    assert not any("bulk_" in a or "bulk_" in b for a, b in pairs)


def test_rename_is_followed_to_the_current_name(tmp_path: pathlib.Path) -> None:
    """History before a rename counts under the file's current name, so a moved
    module keeps its co-change record instead of splitting it across two names."""
    _init_repo(tmp_path)
    _commit(tmp_path, "chore: seed", {"pkg_a/mod.py": "x = 0\n", "pkg_b/mod.py": "x = 0\n"})
    for i in range(2):
        _commit(
            tmp_path,
            f"fix(ab): touch both {i}",
            {"pkg_a/mod.py": f"x = {i + 1}\n", "pkg_b/mod.py": f"x = {i + 1}\n"},
        )
    _git(tmp_path, "mv", "pkg_a/mod.py", "pkg_a/renamed.py")
    (tmp_path / "pkg_b/mod.py").write_text("x = 99\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "--quiet", "-m", "refactor: rename mod.py")

    report = _run(tmp_path, "--min-support", "2", "--min-confidence", "0.5")

    pairs = {(row["a"], row["b"]): row for row in report["strong_pairs"]}
    row = pairs[("pkg_a/renamed.py", "pkg_b/mod.py")]
    # seed + two "touch both" commits (under the old name) + the rename commit.
    assert row["c"] == 4
    assert row["n_a"] == 4
    assert row["n_b"] == 4
    assert not any("pkg_a/mod.py" in (a, b) for a, b in pairs)


def test_pair_with_a_deleted_file_is_not_reported(tmp_path: pathlib.Path) -> None:
    """A file gone from the tip has no owner left to fix, so its pairs drop out of
    Metric B while its commits still count toward Metric A's spread."""
    _init_repo(tmp_path)
    for i in range(3):
        _commit(
            tmp_path,
            f"fix(ab): touch all {i}",
            {
                "pkg_a/mod.py": f"x = {i}\n",
                "pkg_b/mod.py": f"x = {i}\n",
                "pkg_c/gone.py": f"x = {i}\n",
            },
        )
    _git(tmp_path, "rm", "--quiet", "pkg_c/gone.py")
    _git(tmp_path, "commit", "--quiet", "-m", "refactor: delete gone.py")

    report = _run(tmp_path, "--min-support", "2", "--min-confidence", "0.5")

    pairs = {(row["a"], row["b"]) for row in report["strong_pairs"]}
    assert pairs == {("pkg_a/mod.py", "pkg_b/mod.py")}
    assert report["spread_by_type"]["fix"]["p50"] == 3


# --- JSON shape + exit code ----------------------------------------------


def test_json_output_shape(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "fix(a): seed", {"pkg_a/mod.py": "x = 1\n"})

    report = _run(tmp_path)

    assert set(report) == {
        "repo",
        "branch",
        "window",
        "head",
        "min_support",
        "min_confidence",
        "commit_count",
        "spread_by_type",
        "fix_wide_count",
        "fix_count",
        "fix_wide_share",
        "widest_fix",
        "paired_commit_count",
        "strong_pairs",
        "package_pairs",
    }
    assert report["commit_count"] == 1
    assert isinstance(report["spread_by_type"], dict)
    for stats in report["spread_by_type"].values():
        assert set(stats) == {"n", "p50", "p90"}
    assert isinstance(report["strong_pairs"], list)
    assert isinstance(report["package_pairs"], list)
    assert isinstance(report["widest_fix"], list)


def test_exit_code_zero_on_success(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "fix(a): seed", {"pkg_a/mod.py": "x = 1\n"})

    code, output = _run_capture(tmp_path)

    assert code == 0
    assert "# Locality co-change report" in output


def test_markdown_report_has_a_header_and_both_metric_sections(tmp_path: pathlib.Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "fix(a): seed", {"pkg_a/mod.py": "x = 1\n"})

    code, output = _run_capture(tmp_path)

    assert code == 0
    assert "# Locality co-change report" in output
    assert "Metric A" in output
    assert "Metric B" in output
    assert "window:" in output
    assert "thresholds:" in output


# --- smoke test against this repo's real history --------------------------


def test_smoke_against_the_real_repo() -> None:
    """No synthetic repo: run against this checkout's real first-parent history.

    Reads `HEAD`, not `main`: a CI checkout has no local `main` branch.
    """
    code, output = _run_capture(None, "--commits", "50", "--branch", "HEAD")

    assert code == 0
    assert "# Locality co-change report" in output
