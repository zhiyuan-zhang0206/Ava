"""Env decode round-trips and warnings for the served config values, plus AVA_TIMEZONE validation; split from base/tests/test_config.py (task #4922)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import base.log
from base import config


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


def test_current_field_values_coerces_secretstr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A SecretStr field read from .env must come back a SecretStr, not a bare str
    — `.get_secret_value()` consumers crash on a plain str."""
    from pydantic import SecretStr

    from base.host.env import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"anthropic_api_key": "sk-ant-abc"}, set())

    secret = config.current_field_values()["anthropic_api_key"]
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == "sk-ant-abc"


def test_current_field_values_coerces_bool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A bool field written to .env round-trips through the field type unchanged."""
    from base.host.env import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"trace_enabled": True}, set())

    assert config.current_field_values()["trace_enabled"] is True


@pytest.mark.usefixtures("served_gateway_home")
def test_bootstrap_serves_comma_list_not_repr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A NoDecode comma-list field set in .env reaches an agent as the raw "a,b"
    env text, not a Python list repr (which the agent would split into garbage)."""
    from base.host.env import runtime_config as rt
    from base.host.env.dotenv_file import upsert_env

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"skills_to_inject_into_system_prompt": ["alpha", "beta"]}, set())
    upsert_env(tmp_path / ".env", {"AVA_DB_URL": str(config.settings.data_plane.db_url)})

    vals = config.bootstrap_config_values()
    assert vals["AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT"] == "alpha,beta"


def test_current_field_values_decodes_nodecode_comma_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A NoDecode comma-list field reads back as a list (split like the model's
    _split_comma_list validator), not a silently mis-typed raw string."""
    from base.host.env import runtime_config as rt

    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"skills_to_inject_into_system_prompt": ["alpha", "beta"]}, set())

    v = config.current_field_values()["skills_to_inject_into_system_prompt"]
    assert v == ["alpha", "beta"]


def test_current_field_values_decodes_nodecode_comma_list_without_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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

    values = config.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert values["im_send_retry_delays"] == [0.5, 1.0]
    assert all(isinstance(delay, float) for delay in values["im_send_retry_delays"])
    assert rec.warnings == []


def test_current_field_values_decodes_json_array_spelling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The JSON-array spelling the model validators also accept must decode
    through the panel path too — both spellings, not just the comma list."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_disabled_adapters": '["weixin", "feishu"]'}, set())

    values = config.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert rec.warnings == []


def test_current_field_values_decodes_empty_nodecode_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An empty NoDecode list value decodes to [] (nothing disabled), matching
    Settings construction."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_disabled_adapters": []}, set())

    values = config.current_field_values()

    assert values["im_disabled_adapters"] == []
    assert rec.warnings == []


def test_auth_middleware_set_roundtrips_through_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    assert config.current_field_values()["auth_middleware_enabled"] is False


def test_retired_env_aliases_are_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retired names are inert: a stale key left in an environment no longer
    reaches its field (defaults / the None sentinel survive) — the settings
    never read the old spelling."""
    from base.config.agent import AgentSettings
    from base.config.agent_eval import AgentEvalSettings
    from base.config.alerts import AlertsSettings
    from base.config.gateway import GatewaySettings
    from base.config.services import ServiceSettings

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
                from base.config.agent_eval import AgentEvalSettings
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


def test_current_field_values_warns_on_undecodable_env_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A .env value the field annotation cannot decode falls back to the boot
    value WITH a warning naming the key — never silently: the bad line stays in
    the file and the next process start's Settings construction will fail on it,
    so the operator must hear about it at panel-read time (audit round-2
    config.md P2)."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"trace_enabled": "banana"}, set())

    val = config.current_field_values()["trace_enabled"]

    assert val is True  # boot-time fallback (the field default)
    assert len(rec.warnings) == 1
    assert "AVA_TRACE_ENABLED" in rec.warnings[0]
    assert "banana" not in rec.warnings[0]


def test_current_field_values_warns_on_bad_nodecode_list_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A genuinely undecodable NoDecode list value (non-numeric delays) warns
    and falls back to the boot-time value — never a wrong-typed string split
    (the old fallback served ["banana", "apple"] for a list[float] field)."""
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    rt.write_fields({"im_send_retry_delays": "banana,apple"}, set())

    values = config.current_field_values()

    assert values["im_send_retry_delays"] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert len(rec.warnings) == 1
    assert "AVA_IM_SEND_RETRY_DELAYS" in rec.warnings[0]
    assert "banana,apple" not in rec.warnings[0]


def test_current_field_values_silently_serves_boot_db_url_for_agent_profile_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """#4332: an agent-profile process at the default home fresh-reading the
    owner AVA_DB_URL line hits the deliberate guard refusal — an expected
    topology, not a decode failure. It must serve the boot-time value (the
    launcher-injected runner projection) with NO warning and only a debug
    note; the old path warned on every panel read / agent send, while the
    guard's fail-fast for a MISSING projection is unchanged."""
    from base.config import data_plane
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    monkeypatch.setattr(data_plane, "_unit_home", lambda: Path.home() / ".ava")
    monkeypatch.setenv(config.AVA_PROCESS_PROFILE_ENV, "agent")
    # The session singleton carries an empty cluster secret (single-box
    # default); a real agent-profile process has one set — it is what turns
    # the owner URL into the guard refusal.
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "test-cluster-secret")

    boot = config.current_field_values()["db_url"]
    rt.write_fields({"db_url": "postgresql://ava_main:owner-pw@127.0.0.1:5433/ava"}, set())

    values = config.current_field_values()

    assert values["db_url"] == boot
    assert rec.warnings == []
    assert rec.debugs, "the expected refusal must stay visible in a debug log"


def test_current_field_values_warns_on_bad_db_url_under_agent_profile_conditions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """#4332 precision: the refusal classification must not swallow genuine
    decode failures — a malformed URL in the SAME agent-profile/default-home
    context still warns and falls back to the boot-time value, and the
    warning must not suggest removing the load-bearing .env line."""
    from base.config import data_plane
    from base.host.env import runtime_config as rt

    rec = _patch_logger(monkeypatch)
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    monkeypatch.setattr(data_plane, "_unit_home", lambda: Path.home() / ".ava")
    monkeypatch.setenv(config.AVA_PROCESS_PROFILE_ENV, "agent")
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "test-cluster-secret")

    boot = config.current_field_values()["db_url"]
    rt.write_fields({"db_url": "postgresql://[::1"}, set())

    values = config.current_field_values()

    assert values["db_url"] == boot
    assert len(rec.warnings) == 1
    assert "AVA_DB_URL" in rec.warnings[0]
    assert "fix the line" in rec.warnings[0]
    assert "remove" not in rec.warnings[0]


def test_current_field_values_isolates_bad_env_from_good_file_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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

    values = config.current_field_values()

    assert values["im_disabled_adapters"] == ["weixin", "feishu"]
    assert rec.warnings == []


# ─── AVA_TIMEZONE fails fast at Settings construction ───


def test_timezone_validated_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad AVA_TIMEZONE crashed the first inbound turn of every agent
    (ZoneInfo raises in now_timestamp with no try/except); the validator moves
    the failure to Settings construction, where it is loud and immediate."""
    from pydantic import ValidationError

    from base.config.general import GeneralSettings

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

    from base.config.general import GeneralSettings

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

    from base.config.general import GeneralSettings

    monkeypatch.setitem(os.environ, "AVA_TIMEZONE", "America/Los_Angeles")
    records, sink_id = _capture_loguru_warnings()
    try:
        GeneralSettings()
    finally:
        logger.remove(sink_id)
    assert not any("AVA_TIMEZONE is not set" in r for r in records)
