from pathlib import Path

import pytest

from scripts.ci.pull_requests.classify_paths import main


def lanes(
    scratch: Path, paths: list[str], head_ref: str, capsys: pytest.CaptureFixture[str]
) -> dict[str, str]:
    (scratch / "changed.txt").write_text("\n".join(paths) + "\n")
    (scratch / "base.txt").write_text("HEAD\n")
    main(scratch, head_ref)
    return dict(line.split("=") for line in capsys.readouterr().out.splitlines())


def test_a_backend_change_needs_the_backend_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lanes(tmp_path, ["agent/graph/exec/node.py"], "feature", capsys)["backend"] == "true"


def test_a_merge_queue_branch_always_runs_the_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = lanes(tmp_path, ["README.md"], "trunk-merge/pr-1/abc", capsys)
    assert found["backend"] == "true"


def test_the_output_is_two_lines_for_github_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = lanes(tmp_path, ["agent/graph/exec/node.py"], "feature", capsys)
    assert set(found) == {"frontend", "backend"}
    assert set(found.values()) <= {"true", "false"}
