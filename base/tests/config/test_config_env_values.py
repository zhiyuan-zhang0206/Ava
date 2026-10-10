"""Env decode round-trips and warnings for the served config values, plus AVA_TIMEZONE validation; split from base/tests/test_config.py (task #4922)."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

import base.log
from base import config
from base.config.service_read import ConfigAuthority


@pytest.fixture
def authority(tmp_path: Path) -> ConfigAuthority:
    """The test owns the read model and the file it writes."""
    return ConfigAuthority(config.settings, config.settings, tmp_path / ".env")


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.debugs: list[str] = []

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def debug(self, msg: str) -> None:
        self.debugs.append(msg)


def _patch_logger(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    rec = _RecordingLogger()
    # The warning call sites do `from base.log import logger` at call time,
    # so patching the module attribute is enough.
    monkeypatch.setattr(base.log, "logger", rec)
    return rec


def test_current_field_values_coerces_secretstr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """A SecretStr field read from .env must come back a SecretStr, not a bare str
    — `.get_secret_value()` consumers crash on a plain str."""
    from pydantic import SecretStr

    from base.host.env import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"anthropic_api_key": "sk-ant-abc"}, set())

    secret = authority.current_field_values()["anthropic_api_key"]
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "sk-ant-abc"


def test_current_field_values_coerces_bool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """A bool field written to .env round-trips through the field type unchanged."""
    from base.host.env import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"trace_enabled": True}, set())

    assert authority.current_field_values()["trace_enabled"] is True


@pytest.mark.usefixtures("served_gateway_home")
def test_bootstrap_serves_comma_list_not_repr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """A NoDecode comma-list field set in .env reaches an agent as the raw "a,b"
    env text, not a Python list repr (which the agent would split into garbage)."""
    from base.host.env import runtime_config as rt
    from base.host.env.dotenv_file import upsert_env

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"skills_to_inject_into_system_prompt": ["alpha", "beta"]}, set())
    upsert_env(tmp_path / ".env", {"AVA_DB_URL": str(config.settings.data_plane.db_url)})

    vals = authority.bootstrap_config_values(provider_key_envs=(), plugin_cluster_config="")
    assert vals["AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT"] == "alpha,beta"


def test_current_field_values_decodes_nodecode_comma_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """A NoDecode comma-list field reads back as a list (split like the model's
    _split_comma_list validator), not a silently mis-typed raw string."""
    from base.host.env import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"skills_to_inject_into_system_prompt": ["alpha", "beta"]}, set())

    v = authority.current_field_values()["skills_to_inject_into_system_prompt"]
    assert v == ["alpha", "beta"]


def test_current_field_values_decodes_nodecode_comma_list_without_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """P1 regression: a NoDecode comma-list value is a SUPPORTED .env spelling,
    not a decode failure. Decoding through the owning model must not emit the
    "cannot be decoded" warning — it fired on every panel read / agent spawn
    for AVA_IM_DISABLED_ADAPTERS='weixin,feishu' — and a list[float] field must
    come back floats, not a wrong-typed string split."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields(
        {
            "im_disabled_adapters": ["weixin", "feishu"],
            "im_send_retry_delays": [0.5, 1.0],
        },
        set(),
    )

    values = authority.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert values["im_send_retry_delays"] == [0.5, 1.0]
    assert all(isinstance(delay, float) for delay in values["im_send_retry_delays"])
    assert rec.warnings == []


def test_current_field_values_decodes_json_array_spelling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """The JSON-array spelling the model validators also accept must decode
    through the panel path too — both spellings, not just the comma list."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_disabled_adapters": '["weixin", "feishu"]'}, set())

    values = authority.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert rec.warnings == []


def test_current_field_values_decodes_empty_nodecode_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """An empty NoDecode list value decodes to [] (nothing disabled), matching
    Settings construction."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_disabled_adapters": []}, set())

    values = authority.current_field_values()

    assert values["im_disabled_adapters"] == []
    assert rec.warnings == []


def test_auth_middleware_set_roundtrips_through_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """A `.env` write of auth_middleware_enabled (the e2e harness's knob; the
    config API refuses it) writes the key the model actually reads. The field's alias used to fall back to its upper-cased
    NAME (AUTH_MIDDLEWARE_ENABLED), which a validation_alias-only field never
    listens on — a written value was silently lost and the panel never served
    the file value."""
    from base.host.env import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"auth_middleware_enabled": False}, set())

    text = (tmp_path / ".env").read_text()
    keys = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
    assert keys == {"AVA_AUTH_MIDDLEWARE_ENABLED"}
    assert authority.current_field_values()["auth_middleware_enabled"] is False


def test_retired_env_aliases_are_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retired names are inert: a stale key left in an environment no longer
    reaches its field (defaults / the None sentinel survive) — the settings
    never read the old spelling."""
    from base.config.domains.agent.eval import AgentEvalSettings
    from base.config.domains.agent.settings import AgentSettings
    from base.config.domains.gateway import GatewaySettings
    from base.config.domains.observability.alerts import AlertsSettings
    from base.config.domains.services.settings import ServiceSettings

    for key in (
        "AVA_GATEWAY_URL",
        "AVA_AUTH_MIDDLEWARE_ENABLED",
        "AVA_SECURITY_SCAN_ENABLED",
        "AVA_PERMISSIONS_HELPER_PORT",
        "AVA_AGENT_COMMUNICATION_STYLE",
        "AVA_ALERTS_WEBHOOK_TOKEN",
    ):
        monkeypatch.delitem(os.environ, key, raising=False)
    monkeypatch.setitem(os.environ, "AVA_PRIMARY_GATEWAY_URL", "http://legacy-gw")
    monkeypatch.setitem(os.environ, "AVA_SKIP_AUTH", "true")
    monkeypatch.setitem(os.environ, "AVA_SKIP_SECURITY_SCAN", "true")
    monkeypatch.setitem(os.environ, "AVA_NATIVE_HELPER_PORT", "11111")
    monkeypatch.setitem(os.environ, "AVA_SYSTEM_PROMPT_PROGRESS", "true")
    monkeypatch.setitem(os.environ, "AVA_OPS_ALERTS_WEBHOOK_TOKEN", "tok-legacy")

    assert GatewaySettings().gateway_url == ""
    assert GatewaySettings().auth_middleware_enabled is True
    assert AgentEvalSettings().security_scan_enabled is True
    assert ServiceSettings().permissions_helper_port == 9223
    assert AgentSettings().agent_communication_style is None
    assert AlertsSettings().webhook_token is None


def test_eval_isolation_env_aliases_parse(tmp_path: Path) -> None:
    """The child process receives the aliases before its Settings singleton loads."""
    proc = subprocess.run(  # noqa: S603 -- fixed argv, sys.executable is trusted
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                from base.config.domains.agent.eval import AgentEvalSettings
                config = AgentEvalSettings()
                assert config.eval_isolation is True
                assert config.eval_network_allowlist == ["web", "understand"]
                print("ok")
            """),
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AVA_HOME": str(tmp_path),
            "AVA_CONFIG_FETCH": "skip",
            "AVA_EVAL_ISOLATION": "true",
            "AVA_EVAL_NETWORK_ALLOWLIST": "web, understand",
        },
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# ─── current_field_values warns on an undecodable .env value ───


def test_current_field_values_rejects_undecodable_env_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """Invalid file values fail at the explicit reader without serving boot defaults."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"trace_enabled": "banana"}, set())

    with pytest.raises(ValidationError):
        authority.current_field_values()
    assert rec.warnings == []


def test_current_field_values_rejects_bad_nodecode_list_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """Invalid file values fail at the explicit reader without serving boot defaults."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_send_retry_delays": "banana,apple"}, set())

    with pytest.raises(ValidationError):
        authority.current_field_values()
    assert rec.warnings == []


def test_current_field_values_serves_the_explicit_runner_db_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
) -> None:
    """The default-home agent owns a delivered runner login, not its file's owner URL."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".ava"
    home.mkdir()
    runtime = config.settings.model_copy(deep=True, update={"profile": "agent"})
    runtime.data_plane.cluster_secret = secrets.token_urlsafe(16)
    runtime.data_plane.db_url = "postgresql://ava_g0_runner@127.0.0.1:5433/ava?hostaddr=127.0.0.1"
    authority = ConfigAuthority(runtime, config.settings, home / ".env")
    monkeypatch.setattr(rt, "_ava_home", lambda: home)
    boot = authority.current_field_values()["db_url"]
    rt.write_fields({"db_url": "postgresql://ava_main:owner-pw@127.0.0.1:5433/ava"}, set())

    values = authority.current_field_values()

    assert values["db_url"] == boot
    assert rec.warnings == []
    assert rec.debugs, "the expected projection must stay visible in a debug log"


def test_current_field_values_rejects_bad_db_url_with_runner_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
) -> None:
    """The runner projection never hides malformed file URLs."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".ava"
    home.mkdir()
    runtime = config.settings.model_copy(deep=True, update={"profile": "agent"})
    runtime.data_plane.cluster_secret = secrets.token_urlsafe(16)
    runtime.data_plane.db_url = "postgresql://ava_g0_runner@127.0.0.1:5433/ava?hostaddr=127.0.0.1"
    authority = ConfigAuthority(runtime, config.settings, home / ".env")
    monkeypatch.setattr(rt, "_ava_home", lambda: home)
    rt.write_fields({"db_url": "postgresql://[::1"}, set())

    with pytest.raises(ValidationError):
        authority.current_field_values()
    assert rec.warnings == []


def test_current_field_values_isolates_bad_env_from_good_file_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority: ConfigAuthority
):
    """QA #1090 repro: a good comma-list FILE value must decode even when
    ANOTHER field of the same domain carries a bad ENV value (absent from the
    file). The decode payload covers every field, so model_validate never reads
    os.environ — the old retry did, dropped the good file value, and served
    the boot value instead."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_disabled_adapters": ["weixin", "feishu"]}, set())
    # setitem, not setenv: the lint bans setenv on Settings aliases (the
    # singleton never re-reads env); this plants the bad value for the decode
    # path only.
    monkeypatch.setitem(os.environ, "AVA_IM_SEND_RETRY_DELAYS", "banana")

    values = authority.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert rec.warnings == []


# ─── AVA_TIMEZONE fails fast at Settings construction ───


def test_timezone_validated_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad AVA_TIMEZONE crashed the first inbound turn of every agent
    (ZoneInfo raises in now_timestamp with no try/except); the validator moves
    the failure to Settings construction, where it is loud and immediate."""
    from pydantic import ValidationError

    from base.config.domains.general import GeneralSettings

    monkeypatch.setitem(os.environ, "AVA_TIMEZONE", "Not/A_Timezone")
    with pytest.raises(ValidationError, match="not a valid IANA timezone"):
        GeneralSettings()

    monkeypatch.setitem(os.environ, "AVA_TIMEZONE", "Asia/Shanghai")
    assert GeneralSettings().timezone == "Asia/Shanghai"


def _capture_loguru_warnings() -> tuple[list[str], int]:
    """A loguru sink collecting WARNING+ records; returns (records, sink_id).

    The timezone-default warning fires during the Settings singleton build,
    before any stdlib -> loguru bridge exists, so it goes to loguru directly —
    caplog (stdlib) cannot see it."""
    from loguru import logger

    records: list[str] = []

    class _Sink:
        def write(self, message: str) -> None:
            records.append(message)

    return records, logger.add(_Sink(), level="WARNING", format="{message}")


def test_timezone_default_warns_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing AVA_TIMEZONE must not drift silently onto the
    America/Los_Angeles default: a schedule runner with that default fires cron
    jobs at PT midnight instead of the cluster's midnight (2026-08-21 incident,
    schedule #3). The warning names the missing key so the operator fixes the
    cluster .env instead of discovering the wrong fire time."""
    from loguru import logger

    from base.config.domains.general import GeneralSettings

    monkeypatch.delitem(os.environ, "AVA_TIMEZONE", raising=False)
    records, sink_id = _capture_loguru_warnings()
    try:
        GeneralSettings()
    finally:
        logger.remove(sink_id)
    assert any("AVA_TIMEZONE is not set" in r and "America/Los_Angeles" in r for r in records)


def test_timezone_explicit_value_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit AVA_TIMEZONE (any zone, including PT) is a deliberate
    choice — no warning."""
    from loguru import logger

    from base.config.domains.general import GeneralSettings

    monkeypatch.setitem(os.environ, "AVA_TIMEZONE", "America/Los_Angeles")
    records, sink_id = _capture_loguru_warnings()
    try:
        GeneralSettings()
    finally:
        logger.remove(sink_id)
    assert not any("AVA_TIMEZONE is not set" in r for r in records)
