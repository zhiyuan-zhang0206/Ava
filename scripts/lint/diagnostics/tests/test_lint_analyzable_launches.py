"""Python launches must keep their -c source and -m module readable by test selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint.diagnostics import analyzable_launches
from scripts.structure.placement import ModuleIndex

_FORMATTED = (
    "import subprocess, sys\n"
    "PROBE = 'import {name}'\n"
    "def run(name):\n"
    "    subprocess.run([sys.executable, '-c', PROBE.format(name=name)])\n"
)
_LITERAL = (
    "import subprocess, sys\n"
    "PROBE = 'import sys\\nimport json\\nprint(sys.argv[1])'\n"
    "def run(name):\n"
    "    subprocess.run([sys.executable, '-c', PROBE, name])\n"
)


def _check(tmp_path: Path, source: str) -> list[tuple[int, str]]:
    path = tmp_path / "base" / "tests" / "test_probe.py"
    path.parent.mkdir(parents=True)
    path.write_text(source)
    return analyzable_launches.violations(path, "base/tests/test_probe.py", ModuleIndex(tmp_path))


def test_formatted_source_is_reported(tmp_path: Path) -> None:
    assert _check(tmp_path, _FORMATTED) == [
        (4, "Python -c source is not a literal or single binding")
    ]


def test_literal_source_with_argv_data_passes(tmp_path: Path) -> None:
    assert _check(tmp_path, _LITERAL) == []


def test_an_exempted_launch_names_its_reason(tmp_path: Path) -> None:
    exempt = _FORMATTED.replace(
        "PROBE.format(name=name)])\n",
        "PROBE.format(name=name)])  # launch-ok: the generated source is the subject\n",
    )
    assert _check(tmp_path, exempt) == []


def test_explicit_paths_set_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.py"
    assert analyzable_launches.main([str(missing)]) == 1
    assert "target path(s) not found" in capsys.readouterr().err


def test_an_exemption_may_sit_on_the_comment_line_above(tmp_path: Path) -> None:
    exempt = _FORMATTED.replace(
        "    subprocess.run(",
        "    # launch-ok: the generated source is the subject\n    subprocess.run(",
    )
    assert _check(tmp_path, exempt) == []
