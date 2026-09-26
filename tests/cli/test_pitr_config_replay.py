"""Own committed SQL effects independently of effective post-restart settings."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from cli.commands import _pitr_activation as activation
from cli.commands import _pitr_activation_config as config
from services.pitr.activation_state import ActivationRecord, load_record, record_path, write_record
from shared.config import settings
from tests.cli.test_pitr_activation import _env_apply_fixture


@pytest.fixture
def native_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, ActivationRecord]]:
    # Only conftest's disposable native PostgreSQL; never this host's Ava plane.
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        row = conn.execute("SHOW data_directory").fetchone()
        assert row is not None
        data = Path(str(row[0]))
    assert data.name == "data" and data.parent.name.startswith("ava-pg-")
    original = (data / "postgresql.auto.conf").read_bytes()
    parser = config._auto_conf_entries
    record = _env_apply_fixture(monkeypatch, tmp_path, "OTHER=kept\n")
    (tmp_path / "pg/postgresql.auto.conf").unlink()
    (tmp_path / "pg").rmdir()
    (tmp_path / "pg").symlink_to(data, target_is_directory=True)
    record = replace(
        record,
        pre_activation_auto_conf_b64=base64.b64encode(original).decode(),
        pre_activation_auto_conf_digest=hashlib.sha256(original).hexdigest(),
        rollback_expected_auto_conf_digest=hashlib.sha256(original).hexdigest(),
    )
    write_record(tmp_path, record)
    monkeypatch.setattr(config, "_auto_conf_entries", parser)
    monkeypatch.setattr(
        config,
        "_pg_connection",
        lambda: psycopg.connect(settings.data_plane.db_url, autocommit=True),
    )

    def authorized(_home: Path) -> None:
        return None

    monkeypatch.setattr("shared.release_operation.require_pitr_authorized", authorized)
    try:
        yield tmp_path, record
    finally:
        # No reload occurred; restore the exact fixture configuration for later tests.
        (data / "postgresql.auto.conf").write_bytes(original)


def _effective_mode() -> str:
    with config._pg_connection() as conn:
        row = conn.execute("SHOW archive_mode").fetchone()
        assert row is not None
        return str(row[0])


@pytest.mark.parametrize("window", ["before_sql", "after_sql", "env_intent", "env_effect"])
def test_real_pg_replays_own_sql_while_show_remains_off(
    native_auto: tuple[Path, ActivationRecord], monkeypatch: pytest.MonkeyPatch, window: str
) -> None:
    from shared import envfile

    home, record = native_auto

    # _env_apply_fixture isolates other tests from SQL; restore the real SQL writer here.
    def alter(name: str, value: str) -> None:
        with config._pg_connection() as conn:
            conn.execute(
                sql.SQL("ALTER SYSTEM SET {} = {}").format(sql.Identifier(name), sql.Literal(value))
            )

    assert _effective_mode() == "off"
    writes: list[str] = []

    def interrupted(name: str, value: str) -> None:
        writes.append(name)
        if window != "before_sql":
            alter(name, value)
        if window in {"before_sql", "after_sql"}:
            raise RuntimeError("interrupted SQL")

    original_env = envfile.replace_env_bytes_cas

    def interrupted_env(*args: object, **kwargs: object) -> None:
        if window == "env_effect":
            original_env(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("interrupted env")

    monkeypatch.setattr(config, "_alter", interrupted)
    if window.startswith("env_"):
        monkeypatch.setattr(envfile, "replace_env_bytes_cas", interrupted_env)
    with pytest.raises(RuntimeError, match="interrupted"):
        config.apply_wal_config(home, record, {"archive_mode": "on"})
    assert _effective_mode() == "off"
    durable = load_record(home)
    assert durable is not None and durable.config_apply_intent is not None
    if window != "before_sql":
        assert config._persistent_archive_settings(home)["archive_mode"] == "on"
    monkeypatch.setattr(config, "_alter", alter)
    monkeypatch.setattr(envfile, "replace_env_bytes_cas", original_env)
    result = config.apply_wal_config(home, durable, {"archive_mode": "on"})
    assert result.phase == "wal_restart_pending"
    assert result.config_apply_intent is None
    assert config._persistent_archive_settings(home)["archive_mode"] == "on"
    assert _effective_mode() == "off"
    assert (home / ".env").read_text().count("AVA_PITR_ENABLED=") == 1


@pytest.mark.parametrize("after_sql", [False, True])
@pytest.mark.parametrize("drift", ["comment", "setting"])
def test_real_pg_refuses_unrelated_bytes_before_or_after_owned_sql(
    native_auto: tuple[Path, ActivationRecord],
    monkeypatch: pytest.MonkeyPatch,
    after_sql: bool,
    drift: str,
) -> None:
    home, record = native_auto
    if after_sql:

        def interrupted(name: str, value: str) -> None:
            with config._pg_connection() as conn:
                conn.execute("ALTER SYSTEM SET archive_mode = 'on'")
            raise RuntimeError("interrupted SQL")

        monkeypatch.setattr(config, "_alter", interrupted)
        with pytest.raises(RuntimeError, match="interrupted SQL"):
            config.apply_wal_config(home, record, {"archive_mode": "on"})
        captured = load_record(home)
        assert captured is not None
        record = captured
    path = home / "pg/postgresql.auto.conf"
    if drift == "setting":
        config._alter_restore("work_mem", "64MB")
    else:
        path.write_bytes(path.read_bytes() + b"# unowned comment\n")
    before, env = path.read_bytes(), (home / ".env").read_bytes()
    business = record_path(home).read_bytes()

    def refuse_alter(_name: str, _value: str) -> None:
        raise AssertionError("SQL must not run after unowned config drift")

    monkeypatch.setattr(config, "_alter", refuse_alter)
    with pytest.raises(RuntimeError, match=r"owned|differs"):
        config.apply_wal_config(home, record, {"archive_mode": "on"})
    assert path.read_bytes() == before and (home / ".env").read_bytes() == env
    assert record_path(home).read_bytes() == business


def test_real_pg_sql_postimage_preserves_unrelated_and_escaped_values(
    native_auto: tuple[Path, ActivationRecord],
) -> None:
    home, _record = native_auto
    with config._pg_connection() as conn:
        conn.execute("ALTER SYSTEM SET work_mem = '64MB'")
    value = "test -f 'path\\file' && true"
    intent = config._auto_intent(home, "archive_command", value, config._auto_digest(home))
    config.complete_auto_intent(home, intent, config._alter_restore)
    assert dict(config._auto_conf_entries(home))["work_mem"] == "64MB"
    assert config._persistent_archive_settings(home)["archive_command"] == value
    reset = config._auto_intent(home, "archive_command", "__ABSENT__", intent["desired_digest"])
    config.complete_auto_intent(home, reset, config._alter_restore)
    assert config._persistent_archive_settings(home)["archive_command"] == "__ABSENT__"


def test_rollback_accepts_only_exact_post_alter_crash_receipt(
    native_auto: tuple[Path, ActivationRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, record = native_auto

    def interrupted(_name: str, _value: str) -> None:
        with config._pg_connection() as conn:
            conn.execute("ALTER SYSTEM SET archive_mode = 'on'")
        raise RuntimeError("interrupted SQL")

    monkeypatch.setattr(config, "_alter", interrupted)
    with pytest.raises(RuntimeError, match="interrupted SQL"):
        config.apply_wal_config(home, record, {"archive_mode": "on"})
    durable = load_record(home)
    assert durable is not None
    activation._require_owned_rollback_config(home, durable)
    path = home / "pg/postgresql.auto.conf"
    path.write_bytes(path.read_bytes() + b"work_mem = '64MB'\n")
    with pytest.raises(RuntimeError, match="exact owned SQL postimage"):
        activation._require_owned_rollback_config(home, durable)


def test_real_pg_explicit_rollback_continues_after_unjournaled_apply(
    native_auto: tuple[Path, ActivationRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, record = native_auto

    def interrupted(_name: str, _value: str) -> None:
        with config._pg_connection() as conn:
            conn.execute("ALTER SYSTEM SET archive_mode = 'on'")
        raise RuntimeError("interrupted SQL")

    monkeypatch.setattr(config, "_alter", interrupted)
    with pytest.raises(RuntimeError, match="interrupted SQL"):
        config.apply_wal_config(home, record, {"archive_mode": "on"})
    durable = load_record(home)
    assert durable is not None and durable.pre_activation_pg_settings is not None
    monkeypatch.setattr(
        activation, "_read_pg_state", lambda: dict(record.pre_activation_pg_settings or {})
    )
    result = activation._rollback_record(home, durable)
    assert result.phase == "rollback_restart_pending"
    assert result.rollback_setting_intent is None
    assert config._persistent_archive_settings(home) == record.pre_activation_pg_auto_conf
    assert _effective_mode() == "off"
    assert (home / ".env").read_text() == "OTHER=kept\n"
