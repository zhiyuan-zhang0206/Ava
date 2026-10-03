"""`scripts/lint/turn_scoped_config.py` — a typo'd explicit target must fail the gate.

An explicit path argument that does not exist used to scan nothing and exit 0;
it must now report the missing target on stderr and exit 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lint import turn_scoped_config as gate


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd explicit path must fail the gate, not pass as a silent empty scan."""
    good = tmp_path / "ok.py"
    good.write_text("value = 1\n", encoding="utf-8")
    missing = tmp_path / "typo.py"
    assert gate.main([str(missing)]) == 1
    assert str(missing) in capsys.readouterr().err
    assert gate.main([str(good), str(missing)]) == 1


def test_explicit_outside_repo_target_scans_cleanly(tmp_path: Path) -> None:
    """An existing path outside the repo scans instead of dying on the
    repo-relative prefix computation."""
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    assert gate.main([str(outside)]) == 0


def test_a_per_agent_setting_resolved_without_the_agents_overrides_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "bare.py"
    bare.write_text(
        "def f(model):\n"
        '    return resolve_setting("auto_compact_fraction", model=model)\n'
        '\n\ndef g():\n    return get_field("reasoning_effort")\n',
        encoding="utf-8",
    )
    assert gate.main([str(bare)]) == 1
    err = capsys.readouterr().err
    assert "bare.py:2" in err
    assert "bare.py:6" in err


def test_the_agents_overrides_and_non_per_agent_settings_pass(tmp_path: Path) -> None:
    fine = tmp_path / "fine.py"
    fine.write_text(
        "def f(model, agent):\n"
        "    resolve_setting(\n"
        '        "auto_compact_fraction", model=model, overrides=agent.overrides\n'
        "    )\n"
        '    return resolve_setting("llm_retry_max_attempts", model=model)\n',
        encoding="utf-8",
    )
    assert gate.main([str(fine)]) == 0
