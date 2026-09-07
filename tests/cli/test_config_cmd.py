"""`ava config get/set/unset` — the thin client over GET/PUT /api/config.

The gateway calls (`_get_config` / `_put_config`) are monkeypatched, so these
tests exercise the client-side logic: key resolution (env-var or field name),
value coercion by field type, the merge-patch delta (only changed keys; an unset
is an explicit null), the read-only / unknown-key guards, and the restart hint.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cli.commands import config as cfg
from shared import runtime_config
from shared.api_contracts.config import (
    ConfigFieldView,
    ConfigFieldWriteResult,
    ConfigView,
    ConfigWriteResult,
)


def _field(
    name: str,
    env_var: str,
    field_type: str,
    current_value: object,
    writable: bool,
    scope: str,
    restart_required: str,
    remote_writable: bool | None = None,
) -> ConfigFieldView:
    """A ConfigFieldView with the CLI-relevant fields set + inert defaults for the
    rest — built from the real schema (not a partial dict) so the test fails loudly
    if the wire model drops/renames a field the client reads."""
    return ConfigFieldView(
        name=name,
        field_type=field_type,
        current_value=current_value,
        default_value=None,
        description="",
        group="",
        capability="common",
        restart_required=restart_required,
        writable=writable,
        sensitive=False,
        env_var=env_var,
        scope=scope,
        remote_writable=writable if remote_writable is None else remote_writable,
        per_agent=False,
    )


def _view(raw_overrides: dict[str, Any] | None = None) -> ConfigView:
    """A canned ConfigView with a cluster field, a host int field, and a read-only field."""
    return ConfigView(
        fields=[
            _field("llm_model", "AVA_MODEL", "string", "old", True, "cluster-default", "agent"),
            _field("ops_concurrency", "AVA_OPS_CONCURRENCY", "int", 2, True, "host", "ops"),
            _field(
                "db_url", "AVA_DB_URL", "string", "postgresql://x", False, "cluster-pinned", "all"
            ),
        ],
        raw_overrides=raw_overrides or {},
        machine_capabilities=["gateway", "agent-runner"],
    )


@pytest.fixture
def captured_put(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the PUT body + return an applied result; seed GET with a view."""
    state: dict[str, Any] = {"body": None, "machine": None, "view": _view()}

    monkeypatch.setattr(cfg, "_get_config", lambda _machine: state["view"])  # pyright: ignore[reportUnknownArgumentType]

    def _fake_put(body: dict[str, Any], machine: str | None) -> ConfigWriteResult:
        state["body"] = body
        state["machine"] = machine
        return ConfigWriteResult(applied=True, results={}, restart_required=["agent"])

    monkeypatch.setattr(cfg, "_put_config", _fake_put)
    return state


def test_set_by_env_var_name(captured_put: dict[str, Any]) -> None:
    rc = cfg.cmd_config_set(["AVA_MODEL=new"], machine=None)
    assert rc == 0
    assert captured_put["body"] == {"llm_model": "new"}


def test_set_by_field_name(captured_put: dict[str, Any]) -> None:
    rc = cfg.cmd_config_set(["llm_model=new"], machine=None)
    assert rc == 0
    assert captured_put["body"] == {"llm_model": "new"}


def test_set_coerces_int(captured_put: dict[str, Any]) -> None:
    rc = cfg.cmd_config_set(["AVA_OPS_CONCURRENCY=4"], machine=None)
    assert rc == 0
    assert captured_put["body"] == {"ops_concurrency": 4}  # int, not "4"


def test_set_list_field_writes_bare_env_and_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A list-backed CLI value lands as a bare comma list that the consuming
    Settings model parses back into the declared list type."""
    from shared.config.services import ServiceSettings

    view = _view()
    view.fields.append(
        _field(
            "im_disabled_adapters",
            "AVA_IM_DISABLED_ADAPTERS",
            "string",
            [],
            True,
            "cluster-pinned",
            "agent",
        )
    )
    monkeypatch.setattr(cfg, "_get_config", lambda _machine: view)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)

    def _persist(body: dict[str, Any], _machine: str | None) -> ConfigWriteResult:
        runtime_config.write_fields(body, set())
        return ConfigWriteResult(applied=True, results={}, restart_required=["agent"])

    monkeypatch.setattr(cfg, "_put_config", _persist)

    rc = cfg.cmd_config_set(["AVA_IM_DISABLED_ADAPTERS=weixin,feishu"], machine=None)

    assert rc == 0
    assert (tmp_path / ".env").read_text() == "AVA_IM_DISABLED_ADAPTERS=weixin,feishu\n"
    written = runtime_config.read_env_aliases()["AVA_IM_DISABLED_ADAPTERS"]
    parsed = ServiceSettings.model_validate({"AVA_IM_DISABLED_ADAPTERS": written})
    assert parsed.im_disabled_adapters == ["weixin", "feishu"]


def test_set_sends_only_the_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    """A set sends ONLY the changed key — not the whole override set. Absent keys
    are left untouched server-side (merge semantics), so an existing override
    (llm_model here) need not be echoed back and can't be clobbered by a stale GET."""
    state: dict[str, Any] = {"body": None}
    monkeypatch.setattr(cfg, "_get_config", lambda _machine: _view({"llm_model": "keep"}))  # pyright: ignore[reportUnknownArgumentType]

    def _fake_put(body: dict[str, Any], _machine: str | None) -> ConfigWriteResult:
        state["body"] = body
        return ConfigWriteResult(applied=True, results={}, restart_required=[])

    monkeypatch.setattr(cfg, "_put_config", _fake_put)
    rc = cfg.cmd_config_set(["AVA_OPS_CONCURRENCY=4"], machine=None)
    assert rc == 0
    assert state["body"] == {"ops_concurrency": 4}  # only the delta; llm_model not echoed


def test_set_read_only_field_rejected(captured_put: dict[str, Any]) -> None:
    rc = cfg.cmd_config_set(["AVA_DB_URL=postgresql://y"], machine=None)
    assert rc == 1
    assert captured_put["body"] is None  # no PUT issued


def test_set_unknown_key_rejected(captured_put: dict[str, Any]) -> None:
    rc = cfg.cmd_config_set(["NOT_A_KEY=x"], machine=None)
    assert rc == 1
    assert captured_put["body"] is None


def test_set_missing_equals_rejected(captured_put: dict[str, Any]) -> None:
    rc = cfg.cmd_config_set(["AVA_MODEL"], machine=None)
    assert rc == 1
    assert captured_put["body"] is None


def test_set_bad_int_clean_error(captured_put: dict[str, Any]) -> None:
    """A non-numeric value for an int field gives a clean error (rc 1), not an
    uncaught ValueError, and issues no PUT."""
    rc = cfg.cmd_config_set(["AVA_OPS_CONCURRENCY=not-a-number"], machine=None)
    assert rc == 1
    assert captured_put["body"] is None


def test_unset_sends_explicit_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset sends {field: None} — deletion is explicit, never an omission, so
    only the named key is dropped and nothing else is disturbed."""
    state: dict[str, Any] = {"body": None}
    monkeypatch.setattr(
        cfg,
        "_get_config",
        lambda _machine: _view({"llm_model": "x", "ops_concurrency": 4}),  # pyright: ignore[reportUnknownArgumentType]
    )

    def _fake_put(body: dict[str, Any], _machine: str | None) -> ConfigWriteResult:
        state["body"] = body
        return ConfigWriteResult(applied=True, results={}, restart_required=[])

    monkeypatch.setattr(cfg, "_put_config", _fake_put)
    rc = cfg.cmd_config_unset(["AVA_MODEL"], machine=None)
    assert rc == 0
    assert state["body"] == {"llm_model": None}  # explicit null, not omission


def test_set_machine_is_forwarded(captured_put: dict[str, Any]) -> None:
    cfg.cmd_config_set(["AVA_OPS_CONCURRENCY=4"], machine="runner-2")
    assert captured_put["machine"] == "runner-2"


def test_set_machine_host_field_uses_remote_writable(captured_put: dict[str, Any]) -> None:
    captured_put["view"].fields.append(
        _field(
            "permissions_helper_enabled",
            "AVA_PERMISSIONS_HELPER_ENABLED",
            "bool",
            False,
            False,
            "host",
            "agent",
            remote_writable=True,
        )
    )

    rc = cfg.cmd_config_set(["AVA_PERMISSIONS_HELPER_ENABLED=true"], machine="company-mini")

    assert rc == 0
    assert captured_put["body"] == {"permissions_helper_enabled": True}
    assert captured_put["machine"] == "company-mini"


def test_set_machine_host_field_rejects_remote_read_only(
    captured_put: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    captured_put["view"].fields.append(
        _field(
            "agent_host_health_port",
            "AVA_AGENT_HOST_HEALTH_PORT",
            "int",
            8114,
            True,
            "host",
            "agent",
            remote_writable=False,
        )
    )

    rc = cfg.cmd_config_set(["AVA_AGENT_HOST_HEALTH_PORT=18133"], machine="runner-2")

    assert rc == 1
    assert "AVA_AGENT_HOST_HEALTH_PORT is read-only" in capsys.readouterr().err
    assert captured_put["body"] is None


def test_set_machine_non_host_field_still_uses_writable(
    captured_put: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    captured_put["view"].fields.append(
        _field(
            "read_only_cluster_field",
            "AVA_READ_ONLY_CLUSTER_FIELD",
            "string",
            "old",
            False,
            "cluster-pinned",
            "agent",
            remote_writable=True,
        )
    )

    rc = cfg.cmd_config_set(["AVA_READ_ONLY_CLUSTER_FIELD=new"], machine="runner-2")

    assert rc == 1
    assert "AVA_READ_ONLY_CLUSTER_FIELD is read-only" in capsys.readouterr().err
    assert captured_put["body"] is None


def test_unset_machine_host_field_rejects_remote_read_only(
    captured_put: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    captured_put["view"].fields.append(
        _field(
            "agent_host_health_port",
            "AVA_AGENT_HOST_HEALTH_PORT",
            "int",
            8114,
            True,
            "host",
            "agent",
            remote_writable=False,
        )
    )

    rc = cfg.cmd_config_unset(["AVA_AGENT_HOST_HEALTH_PORT"], machine="runner-2")

    assert rc == 1
    assert "AVA_AGENT_HOST_HEALTH_PORT is read-only" in capsys.readouterr().err
    assert captured_put["body"] is None


@pytest.mark.parametrize("scope", ["host", "cluster-pinned", "cluster-default", "agent"])
@pytest.mark.parametrize("writable", [True, False])
@pytest.mark.parametrize("remote_writable", [True, False])
@pytest.mark.parametrize("remote", [True, False])
def test_cli_field_editable_mirrors_shared_definition(
    scope: str, writable: bool, remote_writable: bool, remote: bool
) -> None:
    """The CLI's wire-view gate and shared.config.editing.field_editable are two
    definitions of one policy; this table pins their agreement so a future
    special case in the shared definition turns this test red (task #2552)."""
    from shared.config import ConfigFieldMeta
    from shared.config.editing import field_editable

    view_field = _field(
        "parity",
        "AVA_PARITY",
        "string",
        None,
        writable,
        scope,
        "",
        remote_writable=remote_writable,
    )
    meta = ConfigFieldMeta(
        "parity",
        "string",
        None,
        None,
        "",
        "",
        "",
        writable=writable,
        sensitive=False,
        env_var="AVA_PARITY",
        scope=scope,
        capability="common",
        remote_writable=remote_writable,
        per_agent=False,
    )
    assert cfg._field_editable(view_field, remote=remote) == field_editable(meta, local=not remote)


def test_unset_machine_host_field_uses_remote_writable(captured_put: dict[str, Any]) -> None:
    captured_put["view"].fields.append(
        _field(
            "permissions_helper_enabled",
            "AVA_PERMISSIONS_HELPER_ENABLED",
            "bool",
            True,
            False,
            "host",
            "agent",
            remote_writable=True,
        )
    )

    rc = cfg.cmd_config_unset(["AVA_PERMISSIONS_HELPER_ENABLED"], machine="company-mini")

    assert rc == 0
    assert captured_put["body"] == {"permissions_helper_enabled": None}
    assert captured_put["machine"] == "company-mini"


def _write_incident_env(home: Path) -> tuple[Path, Path]:
    """Create the OSS restore-proof shape that prevents ``Settings()`` from booting."""
    backup_key = home / "backup.key"
    backup_key.write_bytes(b"k" * 32)
    backup_key.chmod(0o600)
    oss_credentials = home / "oss-uploader.json"
    oss_credentials.write_text(
        json.dumps({"access_key_id": "uploader", "access_key_secret": "fake-secret"})
    )
    oss_credentials.chmod(0o600)
    (home / ".env").write_text(
        "\n".join(
            (
                "AVA_DB_URL=postgresql://ava@127.0.0.1:5432/ava",
                "AVA_REDIS_URL=redis://127.0.0.1:6380/0",
                "AVA_MACHINE_SERVE_GATEWAY=true",
                "AVA_PITR_ENABLED=true",
                "AVA_PITR_BASE_BACKUP_ENABLED=true",
                "AVA_PITR_RESTORE_PROOF_ENABLED=true",
                "AVA_PITR_STORE_BACKEND=oss",
                "AVA_PITR_OSS_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com",
                "AVA_PITR_OSS_BUCKET=test-bucket",
                f"AVA_PITR_BACKUP_KEY_FILE={backup_key}",
                "AVA_PITR_BACKUP_KEY_ID=test-key",
                "AVA_PITR_REPLICATION_DB_URL=postgresql://replicator@127.0.0.1:5432/postgres",
                f"AVA_PITR_OSS_CREDENTIALS_FILE={oss_credentials}",
            )
        )
        + "\n"
    )
    return backup_key, oss_credentials


def _write_valid_gcs_env_with_incomplete_oss_transition(home: Path) -> None:
    """Seed a valid GCS configuration whose writable OSS switch would be invalid."""
    backup_key, oss_credentials = _write_incident_env(home)
    gcs_uploader = home / "gcs-uploader.json"
    gcs_uploader.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "uploader@example.com",
                "project_id": "test-project",
                "private_key_id": "uploader-key",
            }
        )
    )
    gcs_uploader.chmod(0o600)
    gcs_viewer = home / "gcs-viewer.json"
    gcs_viewer.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "viewer@example.com",
                "project_id": "test-project",
                "private_key_id": "viewer-key",
            }
        )
    )
    gcs_viewer.chmod(0o600)
    (home / ".env").write_text(
        "\n".join(
            (
                "AVA_MACHINE_SERVE_GATEWAY=true",
                "AVA_PITR_ENABLED=true",
                "AVA_PITR_BASE_BACKUP_ENABLED=true",
                "AVA_PITR_RESTORE_PROOF_ENABLED=true",
                "AVA_PITR_STORE_BACKEND=gcs",
                "AVA_PITR_GCS_PROJECT=test-project",
                "AVA_PITR_GCS_BUCKET=test-bucket",
                f"AVA_PITR_BACKUP_KEY_FILE={backup_key}",
                "AVA_PITR_BACKUP_KEY_ID=test-key",
                "AVA_PITR_REPLICATION_DB_URL=postgresql://replicator@127.0.0.1:5432/postgres",
                f"AVA_PITR_GCS_CREDENTIALS_FILE={gcs_uploader}",
                f"AVA_PITR_RESTORE_GCS_CREDENTIALS_FILE={gcs_viewer}",
                "AVA_PITR_OSS_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com",
                "AVA_PITR_OSS_BUCKET=test-bucket",
                f"AVA_PITR_OSS_CREDENTIALS_FILE={oss_credentials}",
            )
        )
        + "\n"
    )


def test_cli_module_imports_without_settings(tmp_path: Path) -> None:
    """The local read path remains usable when the current `.env` cannot boot Settings."""
    _write_incident_env(tmp_path)
    repo_root = Path(__file__).parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from cli.commands.config import cmd_config_get; "
                "raise SystemExit(cmd_config_get(None, None, local=True))"
            ),
        ],
        cwd=repo_root,
        env={
            "AVA_CONFIG_FETCH": "skip",
            "AVA_HOME": str(tmp_path),
            "AVA_HOME_OVERRIDE": "1",
            "PATH": os.environ["PATH"],
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "AVA_PITR_OSS_CREDENTIALS_FILE" in result.stdout


@pytest.fixture
def local_env_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    return tmp_path


@pytest.mark.parametrize("key", ("AVA_GATEWAY_URL", "AVA_PRIMARY_GATEWAY_URL"))
def test_gateway_base_reads_local_env_without_settings(
    local_env_home: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.delenv("AVA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("AVA_PRIMARY_GATEWAY_URL", raising=False)
    (local_env_home / ".env").write_text(f"{key}=http://gateway.test:8000\n")

    assert cfg._gateway_base() == "http://gateway.test:8000"


def test_gateway_base_prefers_anchored_home_gateway_url_file_over_aliases(
    local_env_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checkout-anchored home's persisted `gateway_url` identity wins over
    the alias file — the 2026-09-07 worktree incident wrote prod config because
    the alias fallback outranked the home identity."""
    monkeypatch.delenv("AVA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("AVA_PRIMARY_GATEWAY_URL", raising=False)
    home = local_env_home / "anchored-home"
    home.mkdir()
    (home / "gateway_url").write_text("http://own-cluster.test:8000\n")
    monkeypatch.setattr("shared.dotenv_boot.AVA_ENV_PATH", home / ".env")
    (local_env_home / ".env").write_text("AVA_GATEWAY_URL=http://alias.test:9000\n")

    assert cfg._gateway_base() == "http://own-cluster.test:8000"


def test_gateway_base_refuses_unanchored_checkout(
    local_env_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare worktree (no `.ava_home` pointer) must not silently resolve to
    the default home's gateway — refusal with guidance instead."""
    monkeypatch.delenv("AVA_GATEWAY_URL", raising=False)
    monkeypatch.delenv("AVA_PRIMARY_GATEWAY_URL", raising=False)
    monkeypatch.setattr("shared.dotenv_boot.checkout_anchored", lambda: False)
    (local_env_home / ".env").write_text("AVA_GATEWAY_URL=http://prod.test:8000\n")

    with pytest.raises(cfg._ConfigError, match="not anchored"):
        cfg._gateway_base()


def test_put_config_refuses_unanchored_checkout_even_with_env_override(
    local_env_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanchored checkout may read through an explicit env override but
    never write gateway config — no HTTP call happens."""
    monkeypatch.setenv("AVA_GATEWAY_URL", "http://elsewhere.test:8000")
    monkeypatch.setattr("shared.dotenv_boot.checkout_anchored", lambda: False)

    with pytest.raises(cfg._ConfigError, match="refusing to write gateway config"):
        cfg._put_config({"x": "y"}, machine=None)


def test_put_config_refuses_env_override_mismatching_home_identity(
    local_env_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit AVA_GATEWAY_URL that contradicts this home's own gateway
    identity must not write foreign config."""
    home = local_env_home / "anchored-home"
    home.mkdir()
    (home / "gateway_url").write_text("http://own-cluster.test:8000\n")
    monkeypatch.setattr("shared.dotenv_boot.AVA_ENV_PATH", home / ".env")
    monkeypatch.setenv("AVA_GATEWAY_URL", "http://other-cluster.test:9000")

    with pytest.raises(cfg._ConfigError, match="does not match this home's gateway"):
        cfg._put_config({"x": "y"}, machine=None)


def test_local_set_refuses_unanchored_checkout(
    local_env_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The --local write path is the same red-line class: an unanchored
    checkout must not hand-edit the fallback home's .env."""
    (local_env_home / ".env").write_text("OTHER=kept\n")
    monkeypatch.setattr("shared.dotenv_boot.checkout_anchored", lambda: False)

    rc = cfg.cmd_config_set(["ops_concurrency=7"], machine=None, local=True)

    assert rc == 1
    assert "not anchored" in capsys.readouterr().err
    assert runtime_config.read_env_aliases() == {"OTHER": "kept"}


def test_local_get_masks_sensitive(
    local_env_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    credential_path = "/private/fake-oss-credentials.json"
    (local_env_home / ".env").write_text(f"AVA_PITR_OSS_CREDENTIALS_FILE={credential_path}\n")

    rc = cfg.cmd_config_get(None, machine=None, local=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "AVA_PITR_OSS_CREDENTIALS_FILE" in out
    assert "••••••••" in out
    assert credential_path not in out


def test_local_set_validates_candidate_and_rejects_incident_shape(
    local_env_home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The agent runtime exports AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE; inherited
    # here it completes the OSS credential shape and turns this test's expected
    # rejection into a valid patch (task #2552). The candidate validation builds
    # the physical-backup model FRESH from os.environ (shared/config/candidate.py
    # `source_model()`), so the env itself is the seam — not the Settings
    # singleton (which is why setenv/delenv would be a no-op elsewhere, and why
    # the wholesale environ swap is the honest patch here).
    import os

    clean_env = {k: v for k, v in os.environ.items() if k != "AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE"}
    monkeypatch.setattr(os, "environ", clean_env)
    _write_valid_gcs_env_with_incomplete_oss_transition(local_env_home)
    before = (local_env_home / ".env").read_bytes()

    rc = cfg.cmd_config_set(["AVA_PITR_STORE_BACKEND=oss"], machine=None, local=True)

    assert rc == 1
    assert "AVA_PITR_OSS_VIEWER_CREDENTIALS_FILE" in capsys.readouterr().err
    assert (local_env_home / ".env").read_bytes() == before


def test_local_set_applies_valid_patch(
    local_env_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (local_env_home / ".env").write_text("OTHER=kept\n")

    rc = cfg.cmd_config_set(["ops_concurrency=7"], machine=None, local=True)

    assert rc == 0
    assert runtime_config.read_env_aliases()["AVA_OPS_CONCURRENCY"] == "7"
    assert "restart to apply" in capsys.readouterr().out


def test_local_set_can_pin_the_hosted_runner_health_port(
    local_env_home: Path,
) -> None:
    """2026-09-02 (win and wsl both fell back to the shared 8114): the
    agent-host health port was read-only with no official repair surface, so the
    emergency fix was a direct `.env` hand-edit. The field is now host-writable:
    `config set` IS the repair surface. remote_writable stays False, so a remote
    `--machine` set is still refused gateway-side."""
    (local_env_home / ".env").write_text("OTHER=kept\n")

    rc = cfg.cmd_config_set(["AVA_AGENT_HOST_HEALTH_PORT=18133"], machine=None, local=True)

    assert rc == 0
    aliases = runtime_config.read_env_aliases()
    assert aliases["AVA_AGENT_HOST_HEALTH_PORT"] == "18133"
    assert "OTHER" in aliases  # unrelated lines survive the patch


def test_local_unset_removes_key(local_env_home: Path) -> None:
    (local_env_home / ".env").write_text("AVA_OPS_CONCURRENCY=7\n")

    rc = cfg.cmd_config_unset(["AVA_OPS_CONCURRENCY"], machine=None, local=True)

    assert rc == 0
    assert "AVA_OPS_CONCURRENCY" not in runtime_config.read_env_aliases()


def test_local_set_repairs_incident_env(local_env_home: Path) -> None:
    """`set --local` lands a valid patch on the broken `.env` it exists to repair.

    Regression: the backup snapshot forced Settings construction (cluster_tz()
    through the lazy proxy), so any write to an incident-shaped env crashed
    with ValidationError before landing — the headline repair verb was dead.
    Runs in a subprocess because a broken env can only coexist with the lazy
    settings proxy (AVA_CONFIG_FETCH=skip); an in-process import would eagerly
    build Settings from the test process's own env.
    """
    _write_incident_env(local_env_home)
    before = (local_env_home / ".env").read_bytes()
    repo_root = Path(__file__).parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from cli.commands.config import cmd_config_set; "
                "raise SystemExit(cmd_config_set("
                "['AVA_PITR_RESTORE_PROOF_ENABLED=false'], None, local=True))"
            ),
        ],
        cwd=repo_root,
        env={
            "AVA_CONFIG_FETCH": "skip",
            "AVA_HOME": str(local_env_home),
            "AVA_HOME_OVERRIDE": "1",
            "PATH": os.environ["PATH"],
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    # The patch lands (values are stored quoted; compare through the alias
    # view like the sibling valid-env test).
    assert runtime_config.read_env_aliases()["AVA_PITR_RESTORE_PROOF_ENABLED"] == "false"
    # Unrelated incident lines survive the repair write.
    assert "AVA_PITR_OSS_ENDPOINT" in runtime_config.read_env_aliases()
    assert (local_env_home / ".env").read_bytes() != before


def test_set_rejected_result_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host-validated rejection (applied False) surfaces per-field reasons + rc=1."""
    monkeypatch.setattr(cfg, "_get_config", lambda _machine: _view())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        cfg,
        "_put_config",
        lambda _body, _machine: ConfigWriteResult(  # pyright: ignore[reportUnknownArgumentType]
            applied=False,
            results={"ops_concurrency": ConfigFieldWriteResult(ok=False, reason="out of range")},
            restart_required=[],
        ),
    )
    rc = cfg.cmd_config_set(["AVA_OPS_CONCURRENCY=4"], machine=None)
    assert rc == 1


def test_get_single_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cfg, "_get_config", lambda _machine: _view())  # pyright: ignore[reportUnknownArgumentType]
    rc = cfg.cmd_config_get("AVA_MODEL", machine=None)
    assert rc == 0
    out = capsys.readouterr().out
    assert "AVA_MODEL" in out
    assert "old" in out
