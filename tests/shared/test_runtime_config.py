"""`$AVA_HOME/.env` is the single config source of truth.

`shared/runtime_config.py` reads/writes `.env` by each field's env alias, and
runs one one-time migration that retires the former per-machine host override
file (`runtime_config.json`) into `.env`. It is precedence-correct (a key already
set in `.env` is left untouched — env beat the override) and idempotent (the file
is archived after the copy). The cluster-wide DB override table this once mirrored
has been migrated into `.env` and dropped.

`fake_ava_home` redirects `_ava_home()` (and so `.env`) to tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared import runtime_config as rt


@pytest.fixture
def fake_ava_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect _ava_home (and so this unit's .env / legacy files) to tmp_path."""
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    return tmp_path


def _seed_env(home: Path, lines: dict[str, str]) -> None:
    home.joinpath(".env").write_text("".join(f"{k}={v}\n" for k, v in lines.items()))


# ─── .env read / write round-trip ───


class TestEnvRoundtrip:
    def test_write_then_read_alias(self, fake_ava_home: Path) -> None:
        rt.write_fields({"llm_model": "m1"}, set())
        assert rt.read_env_aliases()["AVA_MODEL"] == "m1"

    def test_write_fields_pins_env_file_0600(self, fake_ava_home: Path) -> None:
        """.env is the only on-disk copy of a cluster's secrets — a fresh write
        must land 0600 (the umask default would be 0644, readable by every user
        on the box), matching the 0600 of its own backups (snapshot_env)."""
        rt.write_fields({"anthropic_api_key": "sk-ant-abc"}, set())
        env = fake_ava_home / ".env"
        assert oct(env.stat().st_mode)[-3:] == "600"

        # Idempotent: a later write keeps 0600.
        rt.write_fields({"anthropic_api_key": "sk-ant-def"}, set())
        assert oct(env.stat().st_mode)[-3:] == "600"

    def test_write_fields_fixes_pre_existing_0644_env(self, fake_ava_home: Path) -> None:
        """An old 0644 .env is repaired by the next write (the chmod runs on
        every write, not just file creation)."""
        env = fake_ava_home / ".env"
        env.write_text("AVA_MODEL=m1\n")
        env.chmod(0o644)
        rt.write_fields({"llm_model": "m2"}, set())
        assert oct(env.stat().st_mode)[-3:] == "600"

    def test_env_set_field_names_reflects_file(self, fake_ava_home: Path) -> None:
        rt.write_fields({"llm_model": "m1"}, set())
        names = rt.env_set_field_names()
        assert "llm_model" in names
        assert "ops_concurrency" not in names

    def test_bool_stringified_lowercase(self, fake_ava_home: Path) -> None:
        rt.write_fields({"browser_enabled": True}, set())
        assert rt.read_env_aliases()["AVA_BROWSER_ENABLED"] == "true"

    def test_removal_unsets_only_that_key(self, fake_ava_home: Path) -> None:
        rt.write_fields({"llm_model": "m1", "deepseek_api_key": "sk-x"}, set())
        rt.write_fields({}, {"deepseek_api_key"})
        aliases = rt.read_env_aliases()
        assert "DEEPSEEK_API_KEY" not in aliases
        assert aliases["AVA_MODEL"] == "m1"  # untouched

    def test_absent_env_reads_empty(self, fake_ava_home: Path) -> None:
        assert rt.read_env_aliases() == {}
        assert rt.env_set_field_names() == set()

    def test_write_fields_snapshots_prior_state(self, fake_ava_home: Path) -> None:
        """Every write snapshots the .env first, so a bad write is recoverable —
        the pre-write value survives in backups even after it's overwritten."""
        rt.write_fields({"deepseek_api_key": "sk-orig"}, set())
        rt.write_fields({"deepseek_api_key": "sk-new"}, set())
        backups = sorted((fake_ava_home / "backups" / "env").glob(".env.*"))
        assert any("sk-orig" in b.read_text() for b in backups)
        assert rt.read_env_aliases()["DEEPSEEK_API_KEY"] == "sk-new"

    def test_write_preserves_unrelated_lines(self, fake_ava_home: Path) -> None:
        _seed_env(fake_ava_home, {"AVA_DB_URL": "postgresql://x@127.0.0.1:1/x"})
        rt.write_fields({"llm_model": "m1"}, set())
        aliases = rt.read_env_aliases()
        assert aliases["AVA_DB_URL"] == "postgresql://x@127.0.0.1:1/x"
        assert aliases["AVA_MODEL"] == "m1"


# ─── host override file -> .env (one-time) ───


class TestHostJsonMigration:
    def test_copies_json_into_env_and_archives(self, fake_ava_home: Path) -> None:
        (fake_ava_home / "runtime_config.json").write_text(
            json.dumps({"ops_concurrency": 4, "browser_enabled": True})
        )
        rt.migrate_host_json_to_env()
        aliases = rt.read_env_aliases()
        assert aliases["AVA_OPS_CONCURRENCY"] == "4"
        assert aliases["AVA_BROWSER_ENABLED"] == "true"
        assert not (fake_ava_home / "runtime_config.json").exists()
        assert (fake_ava_home / "runtime_config.json.migrated").exists()

    def test_skips_key_already_in_env(self, fake_ava_home: Path) -> None:
        _seed_env(fake_ava_home, {"AVA_OPS_CONCURRENCY": "9"})
        (fake_ava_home / "runtime_config.json").write_text(json.dumps({"ops_concurrency": 4}))
        rt.migrate_host_json_to_env()
        assert rt.read_env_aliases()["AVA_OPS_CONCURRENCY"] == "9"

    def test_absent_file_noop(self, fake_ava_home: Path) -> None:
        rt.migrate_host_json_to_env()
        assert not (fake_ava_home / "runtime_config.json.migrated").exists()

    def test_malformed_json_left_in_place(self, fake_ava_home: Path) -> None:
        (fake_ava_home / "runtime_config.json").write_text("{ not json")
        rt.migrate_host_json_to_env()
        assert (fake_ava_home / "runtime_config.json").exists()
        assert not (fake_ava_home / "runtime_config.json.migrated").exists()

    def test_idempotent(self, fake_ava_home: Path) -> None:
        (fake_ava_home / "runtime_config.json").write_text(json.dumps({"ops_concurrency": 4}))
        rt.migrate_host_json_to_env()
        rt.migrate_host_json_to_env()  # file now archived -> no-op
        assert rt.read_env_aliases()["AVA_OPS_CONCURRENCY"] == "4"


def test_rename_env_keys_finds_and_keeps_an_export_prefixed_line(fake_ava_home: Path) -> None:
    """A legacy key behind the export prefix is the same key to the parser: the
    rename finds it, keeps the prefix, and leaves lookalikes alone (#2981)."""
    env_path = fake_ava_home / ".env"
    env_path.write_text("export OLD_KEY=v1\nKEEP=1\nexportED_OLD_KEY=v2\n")
    assert rt.rename_env_keys(env_path, {"OLD_KEY": "NEW_KEY"}) == ["OLD_KEY -> NEW_KEY"]
    assert env_path.read_text() == "export NEW_KEY=v1\nKEEP=1\nexportED_OLD_KEY=v2\n"


# ─── migrate_alerts_webhook_token_env_key (legacy alerts key rename) ───


def test_alerts_webhook_token_migration_renames_key(fake_ava_home: Path) -> None:
    """AVA_OPS_ALERTS_WEBHOOK_TOKEN -> AVA_ALERTS_WEBHOOK_TOKEN, value unchanged."""
    _seed_env(fake_ava_home, {"AVA_OPS_ALERTS_WEBHOOK_TOKEN": "tok-123", "KEEP": "1"})
    changed = rt.migrate_alerts_webhook_token_env_key(fake_ava_home / ".env")
    assert changed == ["AVA_OPS_ALERTS_WEBHOOK_TOKEN -> AVA_ALERTS_WEBHOOK_TOKEN"]
    text = (fake_ava_home / ".env").read_text()
    assert "AVA_ALERTS_WEBHOOK_TOKEN=tok-123" in text
    assert "AVA_OPS_ALERTS_WEBHOOK_TOKEN" not in text
    assert "KEEP=1" in text


def test_alerts_webhook_token_migration_canonical_wins_when_both_present(
    fake_ava_home: Path,
) -> None:
    """Both names present -> the canonical name is authoritative, legacy dropped."""
    _seed_env(
        fake_ava_home,
        {"AVA_OPS_ALERTS_WEBHOOK_TOKEN": "tok-old", "AVA_ALERTS_WEBHOOK_TOKEN": "tok-new"},
    )
    changed = rt.migrate_alerts_webhook_token_env_key(fake_ava_home / ".env")
    assert changed == [
        "AVA_OPS_ALERTS_WEBHOOK_TOKEN dropped (AVA_ALERTS_WEBHOOK_TOKEN authoritative)"
    ]
    text = (fake_ava_home / ".env").read_text()
    assert "AVA_ALERTS_WEBHOOK_TOKEN=tok-new" in text
    assert "AVA_OPS_ALERTS_WEBHOOK_TOKEN" not in text


def test_alerts_webhook_token_migration_idempotent_and_absent(fake_ava_home: Path) -> None:
    _seed_env(fake_ava_home, {"AVA_ALERTS_WEBHOOK_TOKEN": "tok-123"})
    assert rt.migrate_alerts_webhook_token_env_key(fake_ava_home / ".env") == []
    assert rt.migrate_alerts_webhook_token_env_key(fake_ava_home / "missing.env") == []
