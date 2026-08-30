"""Pure unit tests for the shared config write-path policy
(`shared/config/editing.py`) — the editability gate, the merge-patch reducer and
the ConfigPatchPlan parser that PUT /api/config and the host-side
config_write_op both consume.

The HTTP behavior-level coverage lives in tests/gateway/test_config_api.py;
these tests lock the policy itself (gate matrix, scope routing, reducer
semantics, scalar coercion) with synthetic metadata, no fixtures.
"""

import pytest

from shared.config import CONFIG_UNCHANGED_SENTINEL, ConfigFieldMeta, get_config_metadata
from shared.config.editing import (
    ConfigPatchPlan,
    coerce_config_scalar,
    field_editable,
    split_reducer_patch,
)


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


# ── field_editable: gate matrix (4 writable × remote_writable corners × 2 scopes × local/remote) ──

# (scope, writable, remote_writable, is_remote, expected editable)
GATE_CASES: list[tuple[str, bool, bool, bool, bool]] = [
    # Cluster / agent scope: editable iff `writable`, regardless of locality.
    ("cluster", True, True, False, True),
    ("cluster", True, True, True, True),
    ("cluster", True, False, False, True),
    (
        "cluster",
        True,
        False,
        True,
        True,
    ),  # writable -> passes the gate; routing sends it to remote_cluster_keys
    ("cluster", False, True, False, False),
    ("cluster", False, True, True, False),
    ("cluster", False, False, False, False),
    ("cluster", False, False, True, False),
    # Host scope: local -> `writable`; remote -> `remote_writable`.
    ("host", True, True, False, True),
    ("host", True, True, True, True),
    ("host", True, False, False, True),
    (
        "host",
        True,
        False,
        True,
        False,
    ),  # tightened: a writable-only host field is not remotely editable
    ("host", False, True, False, False),
    ("host", False, True, True, True),  # heartbeat_enabled shape: remote-only toggle
    ("host", False, False, False, False),
    ("host", False, False, True, False),
]


@pytest.mark.parametrize(
    ("scope", "writable", "remote_writable", "is_remote", "editable"), GATE_CASES
)
def test_gate_matrix(
    scope: str, writable: bool, remote_writable: bool, is_remote: bool, editable: bool
) -> None:
    """The editability gate is exactly the table: scope × locality × the two
    writability flags. Anything else (e.g. a future `or` re-widening the remote
    host gate) fails here."""
    meta = _meta("f", scope=scope, writable=writable, remote_writable=remote_writable)
    assert field_editable(meta, local=not is_remote) is editable


def test_field_editable_consistent_with_metadata() -> None:
    """Every real config field's editability follows the same rule the gate
    implements — a drift lock: if someone flips writable / remote_writable on a
    field (or the gate's rule), this test fails and the change is reviewed
    explicitly. `local` is the Cluster view / own-host edit; `remote` is a
    machine-addressed edit."""
    for meta in get_config_metadata():
        assert field_editable(meta, local=True) is meta.writable, meta.name
        if meta.scope == "host":
            assert field_editable(meta, local=False) is meta.remote_writable, meta.name
        else:
            assert field_editable(meta, local=False) is meta.writable, meta.name


# ── split_reducer_patch ──


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
    """A sensitive field carrying the unchanged-sentinel is neither written nor
    removed — the round-trip protection behind the masked raw_overrides."""
    metas = {"s": _meta("s", sensitive=True)}
    writes, removals = split_reducer_patch({"s": CONFIG_UNCHANGED_SENTINEL}, metas)
    assert writes == {}
    assert removals == set()


def test_reducer_nonsensitive_field_can_hold_literal_sentinel() -> None:
    """The sentinel-skip is gated on `sensitive`, so a non-sensitive field can be
    set to the literal sentinel string."""
    metas = {"lit": _meta("lit")}
    writes, removals = split_reducer_patch({"lit": CONFIG_UNCHANGED_SENTINEL}, metas)
    assert writes == {"lit": CONFIG_UNCHANGED_SENTINEL}
    assert removals == set()


# ── ConfigPatchPlan.parse ──


def _metas() -> dict[str, ConfigFieldMeta]:
    return {
        "cluster_str": _meta("cluster_str"),
        "cluster_int": _meta("cluster_int", field_type="int"),
        "cluster_enum": _meta("cluster_enum", field_type="enum", choices=["x", "y"]),
        "host_str": _meta("host_str", scope="host", remote_writable=True),
    }


def test_parse_routes_by_scope_and_reduces() -> None:
    """Local PUT: host keys go to host_body, cluster keys reduce into
    writes/removals (null = explicit removal)."""
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
    # Violating keys never reach the routed bodies.
    assert plan.host_body == {}
    assert plan.cluster_writes == {"cluster_str": "v"}


def test_parse_remote_keeps_cluster_keys_separate() -> None:
    """Remote PUT: cluster keys are not dropped into writes — they are reported
    as remote_cluster_keys so the handler 400s with the machine-independent
    message (the HTTP layer locks that message)."""
    plan = ConfigPatchPlan.parse({"cluster_str": "v", "host_str": "h"}, _metas(), is_remote=True)
    assert plan.violations == ()
    assert plan.host_body == {"host_str": "h"}
    assert plan.remote_cluster_keys == ("cluster_str",)
    assert plan.cluster_writes == {}
    assert plan.cluster_removals == set()
    assert plan.scalar_error is None


def test_parse_remote_does_not_coerce_host_values() -> None:
    """Host values are coerced/validated host-side by config_write_op — the
    remote parse leaves them untouched."""
    plan = ConfigPatchPlan.parse({"host_str": "h"}, _metas(), is_remote=True)
    assert plan.host_body == {"host_str": "h"}
    assert plan.scalar_error is None


def test_parse_coerces_cluster_scalars() -> None:
    """A cluster scalar is normalized to its typed form before any write."""
    plan = ConfigPatchPlan.parse({"cluster_int": "5"}, _metas(), is_remote=False)
    assert plan.cluster_writes == {"cluster_int": 5}
    assert plan.scalar_error is None


def test_parse_reports_first_bad_scalar() -> None:
    """A malformed cluster scalar is reported atomically — nothing is coerced or
    written, and the error detail names the field."""
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
    """The heartbeat_enabled shape: writable=False + remote_writable=True is
    editable only through a machine-addressed PUT."""
    metas = _metas()
    metas["toggle"] = _meta("toggle", scope="host", writable=False, remote_writable=True)
    local = ConfigPatchPlan.parse({"toggle": False}, metas, is_remote=False)
    remote = ConfigPatchPlan.parse({"toggle": False}, metas, is_remote=True)
    assert local.violations == ("toggle",)
    assert local.host_body == {}
    assert remote.violations == ()
    assert remote.host_body == {"toggle": False}


def test_parse_remote_rejects_writable_only_host_field() -> None:
    """The tightened corner: a writable-but-not-remote_writable host field
    (cross_machine_transfer_backend / require_github_pr / pgbouncer_enabled /
    memory_keep_local shape) is rejected at the gate on a machine-addressed PUT —
    the host-side op would reject it anyway, so the gate now fails before the
    dispatch. Locked at HTTP level by
    test_put_remote_rejects_writable_non_remote_host_field."""
    metas = _metas()
    metas["wonly"] = _meta("wonly", scope="host", writable=True, remote_writable=False)
    plan = ConfigPatchPlan.parse({"wonly": 1}, metas, is_remote=True)
    assert plan.violations == ("wonly",)
    assert plan.host_body == {}


# ── coerce_config_scalar ──


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
