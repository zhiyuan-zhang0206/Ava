"""`config_read_op` and `config_write_op` — host-side config RPC operations.

Also covers `_dispatch` routing for the config_read / config_write op kinds
in `services/agent_ops/daemon.py`.

Tests run entirely in-process; no DB required (config_read_op and
config_write_op only touch this machine's .env + settings metadata).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ops import ops_config as ops
from ops.ops_config import config_read_op, config_write_op
from ops.rpc_schemas import ConfigReadResult, ConfigWriteOpResult
from shared import host_config_validators, runtime_config
from shared.config import get_config_metadata

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_host_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect runtime_config._ava_home to a fresh per-test tmp dir.

    unit_home patches settings.general.ava_home but runtime_config._ava_home reads
    os.environ["AVA_HOME"] directly, so tests that read/write this machine's .env
    need this extra patch for proper isolation.
    """
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _host_field_names() -> list[str]:
    return [m.name for m in get_config_metadata() if m.scope == "host"]


def _sensitive_host_field_names() -> list[str]:
    return [m.name for m in get_config_metadata() if m.scope == "host" and m.sensitive]


_SYNTHETIC_SENSITIVE_HOST = "__test_sensitive_host__"


def _inject_sensitive_host_field(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make the masking path actually reachable.

    No real host-scope field is sensitive today — every sensitive field is
    cluster-pinned — so the old `pytest.skip` guards made these tests
    permanent no-ops on every host, CI included (they never ran, ever; audit
    round-2 cc-docs-tests P2 — the fail-loud assertion in the first revision
    of this fix proved the point by going red). The synthetic field rides the
    real metadata list, so every downstream lookup (metas_by_name,
    host_fields) sees it.
    """
    from shared.config import ConfigFieldMeta

    metas = get_config_metadata()
    field = ConfigFieldMeta(
        name=_SYNTHETIC_SENSITIVE_HOST,
        field_type="str",
        current_value="secret-value",
        default_value=None,
        description="synthetic sensitive host-scope field (masking-path test seam)",
        group="test",
        restart_required="none",
        writable=True,
        sensitive=True,
        env_var="AVA_TEST_SENSITIVE_HOST",
        scope="host",
        capability="common",
        remote_writable=True,
        per_agent=False,
    )
    monkeypatch.setattr(ops, "get_config_metadata", lambda: [*metas, field])
    return _SYNTHETIC_SENSITIVE_HOST


# ---------------------------------------------------------------------------
# config_read_op
# ---------------------------------------------------------------------------


def test_config_read_op_returns_all_host_fields(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config_read_op returns one entry per host-scope field."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = config_read_op()
    host_names = set(_host_field_names())
    assert host_names == set(result.host_fields.keys())


def test_config_read_op_sensitive_field_masked(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sensitive field values are replaced with the mask string."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    # The masking path is dead code without a sensitive host-scope field —
    # inject one (see the helper) instead of skipping/failing on the empty
    # real-world list.
    name = _inject_sensitive_host_field(monkeypatch)
    result = config_read_op()
    entry = result.host_fields[name]
    assert entry.value == "••••••••", f"{name} should be masked but got {entry.value!r}"


def test_config_read_op_overridden_flag_reflects_local_file(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'overridden' is True for a field present in the local runtime_config.json."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    # Set ops_concurrency in this machine's .env
    runtime_config.write_fields({"ops_concurrency": 7}, set())
    result = config_read_op()
    assert result.host_fields["ops_concurrency"].overridden is True
    # A field not in the local file should not be marked overridden
    not_overridden = [n for n in _host_field_names() if n != "ops_concurrency"]
    for name in not_overridden:
        assert result.host_fields[name].overridden is False, f"{name} should not be overridden"


def test_config_read_op_can_enable_for_browser_enabled(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """browser_enabled has a read-time capability hint — can_enable is bool, not None."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    # Monkeypatch the validator so the test is deterministic on any host
    monkeypatch.setattr(
        host_config_validators,
        "read_time_capability",
        lambda name: (  # pyright: ignore[reportUnknownArgumentType]
            host_config_validators.ValidationResult(ok=True) if name == "browser_enabled" else None
        ),
    )
    result = config_read_op()
    entry = result.host_fields["browser_enabled"]
    assert isinstance(entry.can_enable, bool)
    assert entry.can_enable is True
    assert entry.reason is None


def test_config_read_op_can_enable_false_carries_reason(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When read_time_capability returns ok=False the reason is propagated."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(
        host_config_validators,
        "read_time_capability",
        lambda name: (  # pyright: ignore[reportUnknownArgumentType]
            host_config_validators.ValidationResult(ok=False, reason="no display detected")
            if name == "browser_enabled"
            else None
        ),
    )
    result = config_read_op()
    entry = result.host_fields["browser_enabled"]
    assert entry.can_enable is False
    assert entry.reason == "no display detected"


def test_config_read_op_non_gated_field_has_null_can_enable(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fields with no static read-time gate get can_enable=None, reason=None."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = config_read_op()
    # machine_description has no entry in VALIDATORS and no read_time_capability
    entry = result.host_fields["machine_description"]
    assert entry.can_enable is None
    assert entry.reason is None


def test_config_read_op_raw_overrides_omits_sensitive(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """raw_overrides omits keys that are sensitive host fields."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    name = _inject_sensitive_host_field(monkeypatch)
    # The synthetic field cannot go through write_fields (not in the alias
    # map) — inject the .env-override premise directly; the assertion under
    # test is config_read_op's sensitive-filtering of raw_overrides.
    monkeypatch.setattr(
        ops,
        "env_override_values",
        lambda: {name: "secret-val", "ops_concurrency": 3},
    )
    result = config_read_op()
    raw = result.raw_overrides
    assert name not in raw, f"sensitive field {name} must be omitted from raw_overrides"
    assert raw.get("ops_concurrency") == 3


def test_config_read_op_machine_name_in_result(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Result includes the machine name from machine_name()."""
    monkeypatch.setattr(ops, "machine_name", lambda: "my-host")
    result = config_read_op()
    assert result.machine == "my-host"


# ---------------------------------------------------------------------------
# config_write_op
# ---------------------------------------------------------------------------


def test_config_write_op_rejects_unknown_field(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown field name returns ok=False with reason='unknown field'."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = config_write_op({"totally_unknown_field": 42})
    assert result.applied is False
    assert result.results["totally_unknown_field"].ok is False
    assert "unknown field" in (result.results["totally_unknown_field"].reason or "")


def test_config_write_op_rejects_non_host_field(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cluster-scope field is rejected with reason='not a host-scope field'."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = config_write_op({"llm_model": "something"})
    assert result.applied is False
    assert result.results["llm_model"].ok is False
    assert "host-scope" in (result.results["llm_model"].reason or "")


def test_config_write_op_rejects_non_remote_writable(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host field with remote_writable=False is rejected."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    # Find a host-scope field that is NOT remote_writable
    non_writable = [
        m.name for m in get_config_metadata() if m.scope == "host" and not m.remote_writable
    ]
    if not non_writable:
        raise AssertionError(
            "all host fields are remote_writable — the non-remote-writable "
            "rejection path is no longer exercised (config changed)"
        )
    result = config_write_op({non_writable[0]: "any-value"})
    assert result.applied is False
    assert result.results[non_writable[0]].ok is False
    assert "not remotely editable" in (result.results[non_writable[0]].reason or "")


def test_config_write_op_local_accepts_writable_host_field(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writable-but-not-remote_writable host field (a capability field) is editable
    on a local (self) write — `writable` means a human may edit it on its own host."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _f, _v: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    result = config_write_op({"cross_machine_transfer_backend": "none"}, local=True)
    assert result.applied is True
    assert runtime_config.env_set_field_names() == {"cross_machine_transfer_backend"}


def test_config_write_op_remote_rejects_writable_only_host_field(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same field is rejected on a remote write (local defaults False) — the
    gateway may not edit a remote host's non-remote_writable field."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = config_write_op({"cross_machine_transfer_backend": "none"})
    assert result.applied is False
    assert "not remotely editable" in (
        result.results["cross_machine_transfer_backend"].reason or ""
    )


def test_config_write_op_rejects_failing_validator(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value that fails the field validator is rejected."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    # ops_concurrency must be int in [1, 64] — 0 should fail
    result = config_write_op({"ops_concurrency": 0})
    assert result.applied is False
    assert result.results["ops_concurrency"].ok is False
    assert result.results["ops_concurrency"].reason is not None


def test_config_write_op_rejects_unknown_transfer_backend(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backend selector is a closed set: a typo (or a stale value from a
    removed option) is rejected at write time, never persisted to .env."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = config_write_op({"cross_machine_transfer_backend": "scp"}, local=True)
    assert result.applied is False
    assert result.results["cross_machine_transfer_backend"].ok is False
    assert "must be one of" in (result.results["cross_machine_transfer_backend"].reason or "")


def test_config_write_op_atomic_one_bad_writes_nothing(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomic: one bad field + one good field -> nothing written, applied=False."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    # One body mixing a failing field (ops_concurrency out of range) with a
    # passing one (machine_description free text) must write neither — atomicity.
    result = config_write_op({"ops_concurrency": 0, "machine_description": "ok"})
    assert result.applied is False
    # Nothing should be written to .env
    assert runtime_config.read_env_aliases() == {}


def test_config_write_op_all_good_writes_file(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully-valid body writes the local file and returns applied=True."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _field, _val: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    result = config_write_op({"ops_concurrency": 4, "machine_description": "test"})
    assert result.applied is True
    assert runtime_config.env_set_field_names() == {"ops_concurrency", "machine_description"}
    assert runtime_config.read_env_aliases()["AVA_OPS_CONCURRENCY"] == "4"


def test_config_write_op_none_unsets_field(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reducer semantics: a field mapped to None is unset (reverts to default)."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _field, _val: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    config_write_op({"ops_concurrency": 4, "machine_description": "hi"})
    assert runtime_config.env_set_field_names() == {"ops_concurrency", "machine_description"}
    # Explicit None unsets machine_description; ops_concurrency is untouched.
    result = config_write_op({"machine_description": None})
    assert result.applied is True
    assert runtime_config.env_set_field_names() == {"ops_concurrency"}
    assert runtime_config.read_env_aliases()["AVA_OPS_CONCURRENCY"] == "4"


def test_config_write_op_absent_key_left_untouched(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reducer semantics: a key ABSENT from the patch is left as-is. A partial
    write never unsets a field it didn't name — the full-replace footgun that
    once wiped a cluster's secrets is gone."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _field, _val: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    config_write_op({"ops_concurrency": 4, "machine_description": "hi"})
    # A patch touching only ops_concurrency must NOT drop machine_description.
    result = config_write_op({"ops_concurrency": 5})
    assert result.applied is True
    assert runtime_config.env_set_field_names() == {"ops_concurrency", "machine_description"}
    assert runtime_config.read_env_aliases()["AVA_OPS_CONCURRENCY"] == "5"


def test_config_write_op_empty_patch_is_noop(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty patch touches nothing — it does not unset previously-set fields."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _field, _val: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    config_write_op({"ops_concurrency": 4})
    result = config_write_op({})
    assert result.applied is True
    assert runtime_config.env_set_field_names() == {"ops_concurrency"}


def test_config_write_op_does_not_disturb_non_managed_env(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write touches only the fields named in the patch — a connection value
    install/enroll wrote into .env (e.g. AVA_DB_URL) is left alone even when a
    managed field is explicitly unset."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _field, _val: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    isolated_host_home.joinpath(".env").write_text("AVA_DB_URL=postgresql://x@127.0.0.1:1/x\n")
    config_write_op({"ops_concurrency": 4})
    config_write_op({"ops_concurrency": None})  # explicit unset must not touch AVA_DB_URL
    assert runtime_config.read_env_aliases()["AVA_DB_URL"] == "postgresql://x@127.0.0.1:1/x"
    assert "AVA_OPS_CONCURRENCY" not in runtime_config.read_env_aliases()


def test_config_write_op_restart_required_union(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """restart_required is the sorted union over written fields' restart_required values."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _field, _val: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    result = config_write_op({"ops_concurrency": 4, "machine_description": "hi"})
    assert result.applied is True
    # ops_concurrency.restart_required + machine_description.restart_required
    metas = {m.name: m for m in get_config_metadata()}
    expected = sorted(
        {
            v
            for f in ("ops_concurrency", "machine_description")
            for v in [metas[f].restart_required]
            if v
        }
    )
    assert result.restart_required == expected


def test_config_write_op_empty_overrides_applied_no_write(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty overrides dict -> applied=True, nothing written, restart_required=[]."""
    monkeypatch.setattr(ops, "machine_name", lambda: "test-machine")
    result = config_write_op({})
    assert result.applied is True
    assert result.restart_required == []
    assert runtime_config.read_env_aliases() == {}


def test_config_write_op_rejects_cross_field_invalid_candidate(
    isolated_host_home: Path,
) -> None:
    """Removing the Baidu token cannot persist an enabled Baidu PITR candidate."""
    backup_key = isolated_host_home / "backup.key"
    backup_key.write_bytes(b"k" * 32)
    backup_key.chmod(0o600)
    baidu_credentials = isolated_host_home / "baidu-credentials.json"
    baidu_credentials.write_text("{}")
    baidu_credentials.chmod(0o600)
    baidu_token = isolated_host_home / "baidu-token.json"
    baidu_token.write_text("{}")
    baidu_token.chmod(0o600)
    runtime_config.write_fields(
        {
            "pitr_enabled": True,
            "pitr_store_backend": "baidu",
            "pitr_baidu_app_root": "/apps/test",
            "pitr_baidu_credentials_file": baidu_credentials,
            "pitr_baidu_token_file": baidu_token,
            "pitr_backup_key_file": backup_key,
            "pitr_backup_key_id": "test-key",
        },
        set(),
    )
    env_path = isolated_host_home / ".env"
    before = env_path.read_bytes()

    result = config_write_op({"pitr_baidu_token_file": None}, local=True)

    assert result.applied is False
    assert result.results["pitr_baidu_token_file"].ok is False
    assert result.results["pitr_baidu_token_file"].reason is not None
    assert "candidate rejected" in result.results["pitr_baidu_token_file"].reason
    assert env_path.read_bytes() == before


def test_config_write_op_machine_name_in_result(
    isolated_host_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Result includes machine name from machine_name()."""
    monkeypatch.setattr(ops, "machine_name", lambda: "my-host")
    result = config_write_op({})
    assert result.machine == "my-host"


# ---------------------------------------------------------------------------
# _dispatch routing for config_read and config_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_config_read_calls_config_read_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_read kind -> ops.config_read_op, returns completed with its result."""
    from services.agent_ops import daemon

    monkeypatch.setattr(daemon, "_db_pool", object())
    captured: list[bool] = []

    def _fake_config_read() -> ConfigReadResult:
        captured.append(True)
        return ConfigReadResult(machine="x", host_fields={}, raw_overrides={})

    monkeypatch.setattr(daemon.ops_config, "config_read_op", _fake_config_read)
    status, result = await daemon._dispatch("config_read", {})
    assert status == "completed"
    # _dispatch serializes the result model to a JSON dict for the wire.
    assert result["machine"] == "x"
    assert captured == [True]


@pytest.mark.asyncio
async def test_dispatch_config_write_passes_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config_write kind -> ops.config_write_op(payload['overrides']), fail-fast on missing key."""
    from services.agent_ops import daemon

    monkeypatch.setattr(daemon, "_db_pool", object())
    captured: dict[str, Any] = {}

    def _fake_config_write(
        overrides: dict[str, Any], *, local: bool = False
    ) -> ConfigWriteOpResult:
        captured["overrides"] = overrides
        captured["local"] = local
        return ConfigWriteOpResult(machine="x", results={}, applied=True, restart_required=[])

    monkeypatch.setattr(daemon.ops_config, "config_write_op", _fake_config_write)
    status, _result = await daemon._dispatch("config_write", {"overrides": {"ops_concurrency": 2}})
    assert status == "completed"
    assert captured["overrides"] == {"ops_concurrency": 2}


@pytest.mark.asyncio
async def test_dispatch_config_write_missing_overrides_key_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """payload missing 'overrides' key -> failed result (ConfigWritePayload
    validation rejects it, fail-fast, no silent fallback)."""
    from services.agent_ops import daemon

    monkeypatch.setattr(daemon, "_db_pool", object())
    # The config_write arm validates payload into ConfigWritePayload, whose
    # `overrides` is required; a missing key is a caught ValidationError surfaced
    # as a 'failed' op result (the /ops route returns HTTP 200 + status=failed).
    status, result = await daemon._dispatch("config_write", {})  # no 'overrides' key
    assert status == "failed"
    assert "overrides" in str(result["error"])
