import pytest

from base.config import CONFIG_UNCHANGED_SENTINEL, ConfigFieldMeta, get_config_metadata
from base.config.admin.editing import (
    ConfigPatchPlan,
    coerce_config_scalar,
    field_editable,
    split_reducer_patch,
)
from base.config.service_read import ConfigAuthority


def _meta(
    name: str,
    *,
    scope: str = "cluster",
    writable: bool = True,
    sensitive: bool = False,
    remote_writable: bool = False,
    per_agent: bool = False,
    field_type: str = "string",
    choices: list[str] | None = None,
) -> ConfigFieldMeta:
    return ConfigFieldMeta(
        name=name,
        field_type=field_type,
        current_value=None,
        default_value=None,
        description="",
        group="g",
        restart_required="ops",
        writable=writable,
        sensitive=sensitive,
        env_var=name.upper(),
        scope=scope,
        capability="common",
        remote_writable=remote_writable,
        per_agent=per_agent,
        choices=choices,
    )


GATE_CASES: list[tuple[str, bool, bool, bool, bool]] = [
    ("cluster", True, True, False, True),
    ("cluster", True, True, True, True),
    ("cluster", True, False, False, True),
    (
        "cluster",
        True,
        False,
        True,
        True,
    ),
    ("cluster", False, True, False, False),
    ("cluster", False, True, True, False),
    ("cluster", False, False, False, False),
    ("cluster", False, False, True, False),
    ("host", True, True, False, True),
    ("host", True, True, True, True),
    ("host", True, False, False, True),
    (
        "host",
        True,
        False,
        True,
        False,
    ),
    ("host", False, True, False, False),
    ("host", False, True, True, True),
    ("host", False, False, False, False),
    ("host", False, False, True, False),
]


@pytest.mark.parametrize(
    ("scope", "writable", "remote_writable", "is_remote", "editable"), GATE_CASES
)
def test_gate_matrix(
    scope: str, writable: bool, remote_writable: bool, is_remote: bool, editable: bool
) -> None:
    meta = _meta("f", scope=scope, writable=writable, remote_writable=remote_writable)
    assert field_editable(meta, local=not is_remote) is editable


def test_field_editable_consistent_with_metadata(config_authority: ConfigAuthority) -> None:
    for meta in get_config_metadata(authority=config_authority):
        assert field_editable(meta, local=True) is meta.writable, meta.name
        if meta.scope == "host":
            assert field_editable(meta, local=False) is meta.remote_writable, meta.name
        else:
            assert field_editable(meta, local=False) is meta.writable, meta.name


def test_reducer_split_writes_and_removals() -> None:
    metas = {
        "a": _meta("a"),
        "b": _meta("b"),
        "s": _meta("s", sensitive=True),
        "lit": _meta("lit"),
    }
    writes, removals = split_reducer_patch(
        {"a": 1, "b": None, "s": CONFIG_UNCHANGED_SENTINEL, "lit": CONFIG_UNCHANGED_SENTINEL},
        metas,
    )
    assert writes == {"a": 1, "lit": CONFIG_UNCHANGED_SENTINEL}
    assert removals == {"b"}


def test_reducer_sentinel_preserves_sensitive_field() -> None:
    metas = {"s": _meta("s", sensitive=True)}
    writes, removals = split_reducer_patch({"s": CONFIG_UNCHANGED_SENTINEL}, metas)
    assert writes == {}
    assert removals == set()


def test_reducer_nonsensitive_field_can_hold_literal_sentinel() -> None:
    metas = {"lit": _meta("lit")}
    writes, removals = split_reducer_patch({"lit": CONFIG_UNCHANGED_SENTINEL}, metas)
    assert writes == {"lit": CONFIG_UNCHANGED_SENTINEL}
    assert removals == set()


def _metas() -> dict[str, ConfigFieldMeta]:
    return {
        "cluster_str": _meta("cluster_str"),
        "cluster_int": _meta("cluster_int", field_type="int"),
        "cluster_enum": _meta("cluster_enum", field_type="enum", choices=["x", "y"]),
        "host_str": _meta("host_str", scope="host", remote_writable=True),
    }


def test_parse_routes_by_scope_and_reduces() -> None:
    plan = ConfigPatchPlan.parse(
        {"cluster_str": "v", "cluster_int": None, "host_str": "h"}, _metas(), is_remote=False
    )
    assert plan.violations == ()
    assert plan.host_body == {"host_str": "h"}
    assert plan.cluster_writes == {"cluster_str": "v"}
    assert plan.cluster_removals == {"cluster_int"}
    assert plan.remote_cluster_keys == ()
    assert plan.scalar_error is None


def test_parse_reports_unknown_and_readonly_fields() -> None:
    metas = _metas()
    metas["readonly"] = _meta("readonly", writable=False)
    plan = ConfigPatchPlan.parse(
        {"nope": 1, "readonly": 1, "cluster_str": "v"}, metas, is_remote=False
    )
    assert plan.violations == ("nope", "readonly")
    assert plan.host_body == {}
    assert plan.cluster_writes == {"cluster_str": "v"}


def test_parse_remote_keeps_cluster_keys_separate() -> None:
    plan = ConfigPatchPlan.parse({"cluster_str": "v", "host_str": "h"}, _metas(), is_remote=True)
    assert plan.violations == ()
    assert plan.host_body == {"host_str": "h"}
    assert plan.remote_cluster_keys == ("cluster_str",)
    assert plan.cluster_writes == {}
    assert plan.cluster_removals == set()
    assert plan.scalar_error is None


def test_parse_remote_does_not_coerce_host_values() -> None:
    plan = ConfigPatchPlan.parse({"host_str": "h"}, _metas(), is_remote=True)
    assert plan.host_body == {"host_str": "h"}
    assert plan.scalar_error is None


def test_parse_coerces_cluster_scalars() -> None:
    plan = ConfigPatchPlan.parse({"cluster_int": "5"}, _metas(), is_remote=False)
    assert plan.cluster_writes == {"cluster_int": 5}
    assert plan.scalar_error is None


def test_parse_reports_first_bad_scalar() -> None:
    plan = ConfigPatchPlan.parse(
        {"cluster_int": True, "cluster_str": "v"}, _metas(), is_remote=False
    )
    assert plan.scalar_error is not None
    assert "cluster_int" in plan.scalar_error
    assert plan.cluster_writes == {}


def test_parse_rejects_enum_outside_choices() -> None:
    plan = ConfigPatchPlan.parse({"cluster_enum": "z"}, _metas(), is_remote=False)
    assert plan.scalar_error is not None
    assert "cluster_enum" in plan.scalar_error


def test_parse_local_rejects_readonly_host_toggle() -> None:
    metas = _metas()
    metas["toggle"] = _meta("toggle", scope="host", writable=False, remote_writable=True)
    local = ConfigPatchPlan.parse({"toggle": False}, metas, is_remote=False)
    remote = ConfigPatchPlan.parse({"toggle": False}, metas, is_remote=True)
    assert local.violations == ("toggle",)
    assert local.host_body == {}
    assert remote.violations == ()
    assert remote.host_body == {"toggle": False}


def test_parse_remote_rejects_writable_only_host_field() -> None:
    metas = _metas()
    metas["wonly"] = _meta("wonly", scope="host", writable=True, remote_writable=False)
    plan = ConfigPatchPlan.parse({"wonly": 1}, metas, is_remote=True)
    assert plan.violations == ("wonly",)
    assert plan.host_body == {}


def test_coerce_scalar_forms() -> None:
    assert coerce_config_scalar("bool", "true") is True
    assert coerce_config_scalar("bool", "off") is False
    assert coerce_config_scalar("int", "12") == 12
    assert coerce_config_scalar("float", "1.5") == 1.5
    assert coerce_config_scalar("enum", "x", ["x", "y"]) == "x"
    assert coerce_config_scalar("string", "plain") == "plain"
    with pytest.raises(ValueError):
        coerce_config_scalar("int", True)
    with pytest.raises(ValueError):
        coerce_config_scalar("int", 1.5)
    with pytest.raises(ValueError):
        coerce_config_scalar("float", True)
    with pytest.raises(ValueError):
        coerce_config_scalar("bool", "maybe")
    with pytest.raises(ValueError):
        coerce_config_scalar("enum", "z", ["x", "y"])


_AUTHORITY_KEYS = {
    "cluster_secret": "AVA_CLUSTER_SECRET",
    "webhook_token": "AVA_ALERTS_WEBHOOK_TOKEN",
    "mcp_endpoint_enabled": "AVA_MCP_ENDPOINT_ENABLED",
}
_OUTBOUND_CREDENTIALS = {
    "ANTHROPIC_API_KEY",
    "BRAVE_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GLM_API_KEY",
    "JINA_API_KEY",
    "MIMO_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENAI_API_KEY",
    "AVA_FEISHU_APP_SECRET",
    "AVA_TELEGRAM_BOT_TOKEN",
    "AVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
}


@pytest.mark.parametrize("field", sorted(_AUTHORITY_KEYS))
def test_every_config_write_refuses_an_authority_key(
    field: str, config_authority: ConfigAuthority
) -> None:
    metas = {meta.name: meta for meta in get_config_metadata(authority=config_authority)}
    assert metas[field].env_var == _AUTHORITY_KEYS[field]
    for value in ("chosen-by-the-caller-" + "x" * 32, None):
        for is_remote in (False, True):
            plan = ConfigPatchPlan.parse({field: value}, metas, is_remote=is_remote)
            assert plan.violations == (field,)
            assert not plan.cluster_writes and not plan.cluster_removals


def test_every_writable_secret_is_an_outbound_credential(config_authority: ConfigAuthority) -> None:
    writable_secrets = {
        meta.env_var
        for meta in get_config_metadata(authority=config_authority)
        if meta.sensitive and (meta.writable or meta.remote_writable)
    }
    assert writable_secrets == _OUTBOUND_CREDENTIALS
