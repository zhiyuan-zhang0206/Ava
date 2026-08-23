"""`ava config get/set/unset` — the thin client over GET/PUT /api/config.

The gateway calls (`_get_config` / `_put_config`) are monkeypatched, so these
tests exercise the client-side logic: key resolution (env-var or field name),
value coercion by field type, the merge-patch delta (only changed keys; an unset
is an explicit null), the read-only / unknown-key guards, and the restart hint.
"""

from __future__ import annotations

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
        remote_writable=writable,
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
