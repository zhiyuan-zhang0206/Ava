from __future__ import annotations

from pathlib import Path

from base.config import per_agent_field_names
from scripts.lint import no_os_environ, turn_scoped_config


def test_the_env_launcher_reads_every_alias_the_registry_owns(tmp_path: Path) -> None:
    alias = "MEIVA_DB_URL"
    assert alias in no_os_environ.settings_managed_aliases()
    target = tmp_path / "test_x.py"
    target.write_text(
        f'def test_x(monkeypatch):\n    monkeypatch.setenv("{alias}", "x")\n', encoding="utf-8"
    )
    assert no_os_environ.main([str(target)]) == 1


def test_the_turn_scoped_launcher_reads_the_registrys_per_agent_fields(tmp_path: Path) -> None:
    field = sorted(per_agent_field_names())[0]
    target = tmp_path / "bare.py"
    target.write_text(f'def f():\n    return get_field("{field}")\n', encoding="utf-8")
    assert turn_scoped_config.main([str(target)]) == 1
