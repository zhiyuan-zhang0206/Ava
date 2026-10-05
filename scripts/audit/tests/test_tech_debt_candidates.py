"""Hermetic tool-result contracts for the optional candidate scanner."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "tech_debt_candidates.sh"


def _run(
    tmp_path: Path, overrides: dict[str, str], *, frontend: bool = True
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python_dir = tmp_path / ".venv/bin"
    python_dir.mkdir(parents=True)
    if frontend:
        (tmp_path / "ui/web/node_modules").mkdir(parents=True)
    for name in ("uv", "npm", "uvx", "rg", "git"):
        key = name.upper()
        (bin_dir / name).write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "${{{key}_OUT}}"\n'
            f'printf "%s\\n" "${{{key}_ERR:-}}" >&2\n'
            f'exit "${{{key}_CODE:-0}}"\n'
        )
        (bin_dir / name).chmod(0o755)
    python = python_dir / "python"
    python.write_text(
        "#!/bin/bash\n"
        f'if [[ "$1" == - && $# -gt 1 ]]; then exec "{sys.executable}" "$@"; fi\n'
        'if [[ "$1" == scripts/structure/cochange.py ]]; then\n'
        ' printf "%s\\n" "$COCHANGE_OUT"; exit "${COCHANGE_CODE:-0}"\n'
        "fi\n"
        "cat >/dev/null\n"
        'printf "%s\\n" "${DOC_OUT:-(none found)}"\n'
        'exit "${DOC_CODE:-0}"\n'
    )
    python.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
        UV_OUT="[]",
        NPM_OUT="{}",
        UVX_OUT="",
        RG_OUT="",
        RG_CODE="1",
        GIT_OUT="abc123",
        COCHANGE_OUT='{"fix_wide_count":0,"strong_pairs":[]}',
    )
    env.update(overrides)
    return subprocess.run(  # noqa: S603 — fixed repository script and isolated fixture paths
        ["bash", str(SCRIPT), "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_empty_sections_and_optional_skip_are_successful(tmp_path: Path) -> None:
    result = _run(tmp_path, {}, frontend=False)
    assert result.returncode == 0
    assert "[result: empty; exit: 1]" in result.stdout
    assert "[result: skipped; reason: no node_modules]" in result.stdout
    assert "=== Scan complete " in result.stdout


def test_known_tool_findings_are_successful_not_errors(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {
            "RG_CODE": "0",
            "RG_OUT": "base/demo.py:1:TODO",
            "NPM_CODE": "1",
            "NPM_OUT": '{"demo":{"wanted":"2","latest":"3"}}',
            "UVX_CODE": "3",
            "UVX_OUT": "base/demo.py:1: unused demo (90% confidence)",
        },
    )
    assert result.returncode == 0
    assert result.stdout.count("[result: findings;") == 6
    assert "failed" not in result.stdout


@pytest.mark.parametrize(
    "overrides",
    [
        {"RG_CODE": "2", "RG_ERR": "missing scan directory"},
        {"UVX_CODE": "1", "UVX_ERR": "syntax error"},
        {"UVX_CODE": "2", "UVX_ERR": "invalid arguments"},
        {"UV_CODE": "2", "UV_ERR": "registry unavailable"},
        {"NPM_CODE": "1", "NPM_OUT": '{"error":{"code":"EACCES"}}'},
        {"NPM_CODE": "1", "NPM_OUT": "{}"},
        {"NPM_OUT": "not JSON"},
        {"UV_OUT": "{}"},
        {"COCHANGE_OUT": "{}"},
        {"DOC_CODE": "1", "DOC_OUT": "Traceback: syntax error"},
    ],
)
def test_tool_or_parser_failure_marks_report_incomplete(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    result = _run(tmp_path, overrides)
    assert result.returncode == 1
    assert "[result: error;" in result.stdout
    assert "=== Scan incomplete:" in result.stdout
    assert "=== Scan complete " not in result.stdout
    for key, evidence in overrides.items():
        if key.endswith("_ERR"):
            assert evidence in result.stdout
    if (
        overrides.get("NPM_OUT") == "not JSON"
        or overrides.get("UV_OUT") == "{}"
        or overrides.get("COCHANGE_OUT") == "{}"
    ):
        assert "report parse error:" in result.stderr
