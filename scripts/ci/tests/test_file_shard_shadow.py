"""Real pytest proves file plans preserve collection and directory fixtures."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = "scripts.ci.file_shard_shadow"


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed interpreter over temporary test projects
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--rootdir",
            str(root),
            "-c",
            str(root / "pyproject.toml"),
            "-p",
            PLUGIN,
            *args,
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


def project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["--import-mode=importlib"]\n'
        'markers = ["flaky: serial-only cases"]\n'
    )
    (root / ".test_durations").write_text('{"stale.py::test_stale": 999999}')
    for directory in ("tests/a", "tests/b"):
        path = root / directory
        marker = path.name
        path.mkdir(parents=True)
        (path / "conftest.py").write_text(
            "import pytest\n"
            "@pytest.fixture(autouse=True)\n"
            "def scope_marker(request):\n"
            f"    request.node.scope_marker = {marker!r}\n"
        )
        for filename in ("test_first.py", "test_second.py"):
            (path / filename).write_text(
                "import pytest\n"
                '@pytest.mark.parametrize("n", [1, 2])\n'
                "def test_contract(n, request):\n"
                f"    assert request.node.scope_marker == {marker!r}\n"
                "@pytest.mark.flaky\n"
                "def test_serial():\n"
                '    raise AssertionError("flaky case must stay outside the plan")\n'
            )
    (root / "tests/conftest.py").write_text(
        "def pytest_generate_tests(metafunc):\n"
        '    if "generated" in metafunc.fixturenames:\n'
        '        metafunc.parametrize("generated", ["left", "right"])\n'
    )
    (root / "tests/b/test_dynamic.py").write_text(
        "def test_generated(generated, request):\n"
        '    assert generated in {"left", "right"}\n'
        '    assert request.node.scope_marker == "b"\n'
    )


def plan(root: Path) -> tuple[Path, dict[str, Any]]:
    project(root)
    output = root / "plan.json"
    result = run(
        root,
        "--collect-only",
        "-m",
        "not flaky",
        "--file-shard-count",
        "2",
        "--file-shard-plan",
        str(output),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output, json.loads(output.read_text())


def check(
    root: Path, manifest: Path, group: int
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    output = root / f"check-{group}.json"
    result = run(
        root,
        "--collect-only",
        "-m",
        "not flaky",
        "--file-shard-check",
        str(manifest),
        "--file-shard-group",
        str(group),
        "--file-shard-report",
        str(output),
    )
    return result, json.loads(output.read_text())


def test_file_groups_preserve_parameters_dynamic_collection_and_fixture_behavior(
    tmp_path: Path,
) -> None:
    manifest, data = plan(tmp_path)
    nodes = [node for group in data["groups"] for node in group["nodes"]]
    assert len(nodes) == 10
    assert len({node["nodeid"] for node in nodes}) == 10
    assert all("scope_marker" in node["fixtures"] for node in nodes)
    owners: dict[str, int] = {}
    for index, group in enumerate(data["groups"], 1):
        files = sorted({node["file"] for node in group["nodes"]})
        assert not set(files) & owners.keys()
        owners.update(dict.fromkeys(files, index))
        collected, report = check(tmp_path, manifest, index)
        assert collected.returncode == 0, collected.stdout + collected.stderr
        assert report["matched"] is True
        # Exercise fixture bodies too, outside the strictly collect-only plugin mode.
        executed = run(tmp_path, "-m", "not flaky", *files)
        assert executed.returncode == 0, executed.stdout + executed.stderr
    assert len(owners) == 5
    assert sum(group["estimated_seconds"] for group in data["groups"]) == 10


def test_changed_autouse_closure_is_reported_even_when_node_ids_stay_the_same(
    tmp_path: Path,
) -> None:
    manifest, data = plan(tmp_path)
    directory = tmp_path / "tests/a/conftest.py"
    directory.write_text(directory.read_text().replace("def scope_marker", "def changed_scope"))
    group = next(
        index
        for index, shard in enumerate(data["groups"], 1)
        if any(node["file"].startswith("tests/a/") for node in shard["nodes"])
    )
    result, report = check(tmp_path, manifest, group)
    assert result.returncode != 0
    assert report["matched"] is False
    assert report["fixture_changes"]
    assert not report["missing"] and not report["extra"]


def test_added_parameter_is_reported_as_an_unplanned_node(tmp_path: Path) -> None:
    manifest, data = plan(tmp_path)
    file = "tests/a/test_first.py"
    source = tmp_path / file
    source.write_text(source.read_text().replace("[1, 2]", "[1, 2, 3]"))
    group = next(
        index
        for index, shard in enumerate(data["groups"], 1)
        if any(node["file"] == file for node in shard["nodes"])
    )
    result, report = check(tmp_path, manifest, group)
    assert result.returncode != 0
    assert report["extra"] == [file + "::test_contract[3]"]
    assert not report["missing"] and not report["fixture_changes"]


@pytest.mark.parametrize("poison", ["duplicate", "escape", "unknown-field"])
def test_invalid_plan_cannot_select_a_collection(tmp_path: Path, poison: str) -> None:
    manifest, data = plan(tmp_path)
    if poison == "duplicate":
        data["groups"][1]["nodes"].append(data["groups"][0]["nodes"][0])
    elif poison == "escape":
        data["groups"][0]["nodes"][0]["file"] = "../outside.py"
    else:
        data["silently_ignored"] = True
    manifest.write_text(json.dumps(data))
    report = tmp_path / "check.json"
    result = run(
        tmp_path,
        "--collect-only",
        "--file-shard-check",
        str(manifest),
        "--file-shard-group",
        "1",
        "--file-shard-report",
        str(report),
    )
    assert result.returncode == 4
    assert "Invalid shadow plan" in result.stderr
    assert not report.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--file-shard-plan", "plan.json"],
        ["--collect-only", "--file-shard-group", "1"],
        ["--collect-only", "--file-shard-plan", "plan.json", "--file-shard-count", "0"],
        ["--collect-only", "--file-shard-plan", "plan.json", "--splits", "2", "--group", "1"],
        ["--collect-only", "--file-shard-plan", "plan.json", "--file-shard-check", "plan.json"],
    ],
)
def test_shadow_cannot_run_bodies_or_reuse_an_already_split_universe(
    tmp_path: Path, args: list[str]
) -> None:
    project(tmp_path)
    result = run(tmp_path, *args)
    assert result.returncode == 4
    assert not (tmp_path / "plan.json").exists()


def test_zero_known_time_and_unknown_nodes_use_the_same_cost_as_pytest_split(
    tmp_path: Path,
) -> None:
    project(tmp_path)
    durations = {
        "tests/a/test_first.py::test_contract[1]": 0,
        "tests/a/test_first.py::test_contract[2]": 4,
        "stale.py::test_stale": 999999,
    }
    (tmp_path / ".test_durations").write_text(json.dumps(durations))
    output = tmp_path / "plan.json"
    result = run(
        tmp_path,
        "--collect-only",
        "-m",
        "not flaky",
        "--file-shard-count",
        "2",
        "--file-shard-plan",
        str(output),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(output.read_text())
    assert data["unknown_nodes"] == 8
    assert sum(group["estimated_seconds"] for group in data["groups"]) == 20
    assert sum(group["estimated_seconds"] for group in data["baseline"]) == 20


def test_duration_drift_cannot_reuse_the_old_plan(tmp_path: Path) -> None:
    manifest, _ = plan(tmp_path)
    (tmp_path / ".test_durations").write_text("{}")
    output = tmp_path / "report.json"
    output.write_text('{"matched": true}')
    result = run(
        tmp_path,
        "--collect-only",
        "--file-shard-check",
        str(manifest),
        "--file-shard-group",
        "1",
        "--file-shard-report",
        str(output),
    )
    assert result.returncode == 4
    assert "same duration input" in result.stderr
    assert not output.exists()


def test_collection_error_never_writes_a_partial_plan(tmp_path: Path) -> None:
    project(tmp_path)
    (tmp_path / "tests/a/test_broken.py").write_text('raise RuntimeError("collection failed")')
    output = tmp_path / "plan.json"
    output.write_text('{"old_valid_plan": true}')
    result = run(tmp_path, "--collect-only", "--file-shard-plan", str(output))
    assert result.returncode != 0
    assert not output.exists()


def test_report_cannot_overwrite_its_input_plan(tmp_path: Path) -> None:
    manifest, _ = plan(tmp_path)
    original = manifest.read_bytes()
    result = run(
        tmp_path,
        "--collect-only",
        "--file-shard-check",
        str(manifest),
        "--file-shard-group",
        "1",
        "--file-shard-report",
        str(manifest),
    )
    assert result.returncode == 4
    assert "different paths" in result.stderr
    assert manifest.read_bytes() == original


def test_plan_and_report_can_live_outside_the_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    project(checkout)
    output = tmp_path / "plan.json"
    result = run(
        checkout,
        "--collect-only",
        "-m",
        "not flaky",
        "--file-shard-count",
        "2",
        "--file-shard-plan",
        str(output),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result, report = check(checkout, output, 1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["matched"]


def test_configuration_drift_is_rejected_before_collecting_a_group(tmp_path: Path) -> None:
    manifest, _ = plan(tmp_path)
    config = tmp_path / "pyproject.toml"
    config.write_text(config.read_text() + 'python_functions = ["contract_*"]\n')
    output = tmp_path / "report.json"
    result = run(
        tmp_path,
        "--collect-only",
        "-m",
        "not flaky",
        "--file-shard-check",
        str(manifest),
        "--file-shard-group",
        "1",
        "--file-shard-report",
        str(output),
    )
    assert result.returncode == 4
    assert "same pytest configuration" in result.stderr
    assert not output.exists()
