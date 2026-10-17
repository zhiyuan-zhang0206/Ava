"""`$MEIVA_HOME/.env` is the single config source of truth.

`base/config/runtime_config.py` reads/writes `.env` by each field's env alias.

`fake_meiva_home` redirects `_meiva_home()` (and so `.env`) to tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from base.config import runtime_config as rt
from base.host.env import dotenv_file


@pytest.fixture
def fake_meiva_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect _meiva_home (and so this unit's .env) to tmp_path."""
    monkeypatch.setattr(dotenv_file, "unit_home", lambda: tmp_path)
    return tmp_path


def _seed_env(home: Path, lines: dict[str, str]) -> None:
    home.joinpath(".env").write_text("".join(f"{k}={v}\n" for k, v in lines.items()))


# ─── .env read / write round-trip ───


class TestEnvRoundtrip:
    def test_write_then_read_alias(self, fake_meiva_home: Path) -> None:
        rt.write_fields({"llm_model": "m1"}, set())
        assert dotenv_file.read_env_aliases()["MEIVA_MODEL"] == "m1"

    def test_write_fields_pins_env_file_0600(self, fake_meiva_home: Path) -> None:
        """.env is the only on-disk copy of a cluster's secrets — a fresh write
        must land 0600 (the umask default would be 0644, readable by every user
        on the box), matching the 0600 of its own backups (snapshot_env)."""
        rt.write_fields({"anthropic_api_key": "sk-ant-abc"}, set())
        env = fake_meiva_home / ".env"
        assert oct(env.stat().st_mode)[-3:] == "600"

        # Idempotent: a later write keeps 0600.
        rt.write_fields({"anthropic_api_key": "sk-ant-def"}, set())
        assert oct(env.stat().st_mode)[-3:] == "600"

    def test_write_fields_fixes_pre_existing_0644_env(self, fake_meiva_home: Path) -> None:
        """An old 0644 .env is repaired by the next write (the chmod runs on
        every write, not just file creation)."""
        env = fake_meiva_home / ".env"
        env.write_text("MEIVA_MODEL=m1\n")
        env.chmod(0o644)
        rt.write_fields({"llm_model": "m2"}, set())
        assert oct(env.stat().st_mode)[-3:] == "600"

    def test_env_set_field_names_reflects_file(self, fake_meiva_home: Path) -> None:
        rt.write_fields({"llm_model": "m1"}, set())
        names = rt.env_set_field_names()
        assert "llm_model" in names
        assert "ops_concurrency" not in names

    def test_bool_stringified_lowercase(self, fake_meiva_home: Path) -> None:
        rt.write_fields({"browser_enabled": True}, set())
        assert dotenv_file.read_env_aliases()["MEIVA_BROWSER_ENABLED"] == "true"

    def test_removal_unsets_only_that_key(self, fake_meiva_home: Path) -> None:
        rt.write_fields({"llm_model": "m1", "deepseek_api_key": "sk-x"}, set())
        rt.write_fields({}, {"deepseek_api_key"})
        aliases = dotenv_file.read_env_aliases()
        assert "DEEPSEEK_API_KEY" not in aliases
        assert aliases["MEIVA_MODEL"] == "m1"  # untouched

    def test_absent_env_reads_empty(self, fake_meiva_home: Path) -> None:
        assert dotenv_file.read_env_aliases() == {}
        assert rt.env_set_field_names() == set()

    def test_write_fields_snapshots_prior_state(self, fake_meiva_home: Path) -> None:
        """Every write snapshots the .env first, so a bad write is recoverable —
        the pre-write value survives in backups even after it's overwritten."""
        rt.write_fields({"deepseek_api_key": "sk-orig"}, set())
        rt.write_fields({"deepseek_api_key": "sk-new"}, set())
        backups = sorted((fake_meiva_home / "backups" / "env").glob(".env.*"))
        assert any("sk-orig" in b.read_text() for b in backups)
        assert dotenv_file.read_env_aliases()["DEEPSEEK_API_KEY"] == "sk-new"

    def test_write_preserves_unrelated_lines(self, fake_meiva_home: Path) -> None:
        _seed_env(fake_meiva_home, {"MEIVA_DB_URL": "postgresql://x@127.0.0.1:1/x"})
        rt.write_fields({"llm_model": "m1"}, set())
        aliases = dotenv_file.read_env_aliases()
        assert aliases["MEIVA_DB_URL"] == "postgresql://x@127.0.0.1:1/x"
        assert aliases["MEIVA_MODEL"] == "m1"
