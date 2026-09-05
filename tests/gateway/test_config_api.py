"""GET/PUT /api/config integration tests.

Regression: required fields (db_url / redis_url etc. with no default) must not
let FastAPI hit the PydanticUndefined sentinel during response serialization and 500.

PUT scope routing: the self PUT splits the body by ownership scope — cluster
keys are written to this gateway's .env, host keys to this machine's .env (the
single config store); agent-scope fields have writable=False and are rejected by
the writable gate ("unknown or read-only fields"), same as any other read-only field.

Cross-machine (`?machine=`): GET/PUT POST the host section to a remote
agent-runner's ops server. The dispatch is stubbed (AsyncMock on
`dispatch_to_machine`) so these tests stay deterministic without a real remote
host — a known-but-offline host 503s on ClusterOpUnreachable, an unknown name
404s. Capability probes are monkeypatched so headless CI is deterministic.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import config as config_router
from ops import cluster_rpc as _cluster_rpc
from ops.rpc_schemas import ConfigWriteOpResult, FieldWriteResult
from shared import host_config_validators, runtime_config
from shared.config import settings
from shared.machine import machine_name


def test_get_config_serializes_required_fields():
    """Settings fields with no default should serialize default_value as null, rather
    than leaking the PydanticUndefined sentinel and causing the entire response to
    500."""
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "fields" in body
    assert "raw_overrides" in body

    by_name = {f["name"]: f for f in body["fields"]}
    # db_url / redis_url have no default on Settings — the core of this regression
    # test. Tests/evals carry no separate DB URL env var; they self-provision a
    # throwaway native Postgres and override db_url at runtime.
    for name in ("db_url", "redis_url"):
        assert name in by_name, f"{name} should be in ConfigView"
        assert by_name[name]["default_value"] is None
        # current_value comes from .env; the test env makes no strong assumption about the specific value, but it should be str
        assert isinstance(by_name[name]["current_value"], str)


def test_redis_url_masked_does_not_leak_cluster_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """redis_url carries the runtime ACL password as userinfo, so GET /api/config
    must mask it like db_url — the credential must never appear in the response."""
    from ops.ops_config import SENSITIVE_MASK

    monkeypatch.setattr(settings.data_plane, "redis_url", "redis://:supersecret@10.0.0.1:6379/0")
    with TestClient(app) as client:
        resp = client.get("/api/config")
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["redis_url"]["sensitive"] is True
    assert fields["redis_url"]["current_value"] == SENSITIVE_MASK
    assert "supersecret" not in resp.text


def test_config_fields_expose_scope() -> None:
    """Each config field in GET /api/config carries its ownership scope."""
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["db_url"]["scope"] == "cluster-pinned"
    assert fields["llm_model"]["scope"] == "cluster-default"
    assert fields["machine_name"]["scope"] == "host"
    assert fields["sdk_disable"]["scope"] == "agent"


def test_config_fields_expose_capability() -> None:
    """Each config field in GET /api/config carries its owning capability — the
    top-level config-panel section, independent of scope (a cluster-scoped field
    like db_url is still gateway-owned). llm_model is common: the cluster-wide
    spawn default is read by the gateway (spawn pre-select / default-model) AND
    frozen into agents at spawn — owned by neither capability alone."""
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["db_url"]["capability"] == "gateway"
    assert fields["gateway_port"]["capability"] == "gateway"
    assert fields["llm_model"]["capability"] == "common"
    assert fields["ops_concurrency"]["capability"] == "agent-runner"
    assert fields["timezone"]["capability"] == "common"


def test_get_config_exposes_target_machine_capabilities() -> None:
    """GET self carries the gateway's own capability set (machine_role()) so the
    panel can pick which capability sections a remote view renders. Only valid
    capability tokens; the self view is a gateway, so it carries at least that."""
    from shared.machine import machine_role

    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    caps = resp.json()["machine_capabilities"]
    assert isinstance(caps, list)
    # The projection carries the full capability set incl. observability-station
    # (WP1/WP2; a station-capable gateway projects its station token to the
    # panel — task #1945 WP3 verification).
    assert set(caps) <= {"gateway", "agent-runner", "observability-station"}  # pyright: ignore[reportUnknownArgumentType]
    assert set(caps) == set(machine_role())  # pyright: ignore[reportUnknownArgumentType]


def test_enum_field_exposes_type_and_choices() -> None:
    """A Literal[...] config field serializes as field_type "enum" with its
    allowed values in `choices`; a non-enum field carries choices null."""
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200
    fields = {f["name"]: f for f in resp.json()["fields"]}
    enum_field = fields["agent_reply_reminder_cadence"]
    assert enum_field["field_type"] == "enum"
    assert enum_field["choices"] == ["once_per_compaction", "every_time"]
    # A scalar field carries no choices.
    assert fields["llm_model"]["choices"] is None


@pytest.fixture
def _stub_browser_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the browser_enabled read-time capability to a deterministic verdict:
    the aggregate browser_capable() gate reports capable -> can_enable True.

    Patches the predicate as bound in `host_config_validators` so the test does
    not depend on the machine actually having a display / Chrome / npx.
    """
    monkeypatch.setattr(host_config_validators, "browser_capable", lambda: True)


def test_get_self_host_fields_carry_capability_hint(_stub_browser_capability: None) -> None:
    """GET self: host fields carry a can_enable/reason hint where one exists
    (browser_enabled), cluster fields carry null for both."""
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    fields = {f["name"]: f for f in resp.json()["fields"]}

    # browser_enabled is host-scope with a read-time capability gate; the stub
    # makes the verdict deterministic.
    be = fields["browser_enabled"]
    assert be["scope"] == "host"
    assert be["can_enable"] is True
    assert be["reason"] is None

    # A cluster field has no per-host capability hint.
    assert fields["llm_model"]["can_enable"] is None
    assert fields["llm_model"]["reason"] is None
    # A host field with no read-time gate (free-text / numbers) also has null.
    assert fields["ops_concurrency"]["can_enable"] is None


def test_get_self_browser_disabled_when_no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET self with no display: browser_enabled.can_enable is False + a reason."""
    monkeypatch.setattr(host_config_validators, "browser_capable", lambda: False)
    monkeypatch.setattr(host_config_validators, "display_available", lambda: False)
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    be = {f["name"]: f for f in resp.json()["fields"]}["browser_enabled"]
    assert be["can_enable"] is False
    assert be["reason"] == "no display detected"


# ── machines-table seeding helper ──


def _seed_machine(name: str) -> None:
    """Insert a minimal agent-runner row so `_assert_machine_known` finds it.

    role defaults to 'agent-runner'; gateway_url is nullable. The per-test
    TRUNCATE (conftest `_clean_state`) clears it again.
    """
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, gateway_url) VALUES (%s, %s) "
            "ON CONFLICT (name) DO NOTHING",
            (name, "http://remote.invalid:8000"),
        )
        conn.commit()


REMOTE = "remote-host"


def test_get_remote_machine_composes_host_from_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET ?machine=<remote>: host fields reflect the remote config_read result,
    cluster fields reflect local metadata, raw_overrides == the remote host
    overrides."""
    _seed_machine(REMOTE)
    # The GET composer iterates every host-scope meta and indexes the result, so
    # the canned host_fields must answer for all of them — fill the explicit ones
    # plus a neutral stub for the rest.
    explicit_host: dict[str, Any] = {
        "browser_enabled": {
            "value": True,
            "overridden": True,
            "remote_writable": True,
            "can_enable": True,
            "reason": None,
        },
        "ops_concurrency": {
            "value": 16,
            "overridden": True,
            "remote_writable": True,
            "can_enable": None,
            "reason": None,
        },
    }
    canned: dict[str, Any] = {
        "machine": REMOTE,
        "host_fields": _full_host_fields_for_remote(explicit_host),
        "raw_overrides": {"browser_enabled": True, "ops_concurrency": 16},
    }

    enqueue = AsyncMock(return_value=canned)
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.get(f"/api/config?machine={REMOTE}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    fields = {f["name"]: f for f in body["fields"]}

    # host fields reflect the canned remote values
    assert fields["browser_enabled"]["current_value"] is True
    assert fields["browser_enabled"]["can_enable"] is True
    assert fields["ops_concurrency"]["current_value"] == 16
    # cluster fields reflect local metadata (the gateway's own)
    assert fields["llm_model"]["scope"] == "cluster-default"
    assert fields["llm_model"]["can_enable"] is None
    # raw_overrides == the remote host overrides (not the local merged set)
    assert body["raw_overrides"] == {"browser_enabled": True, "ops_concurrency": 16}

    # the dispatch was a config_read on the remote target
    assert enqueue.await_count == 1
    assert enqueue.await_args is not None
    kwargs = enqueue.await_args.kwargs
    assert kwargs["target_machine"] == REMOTE
    assert kwargs["kind"] == "config_read"


def _full_host_fields_for_remote(overrides: dict[str, Any]) -> dict[str, Any]:
    """Build a full host_fields dict for every host-scope meta, taking explicit
    overrides where given and a neutral stub otherwise — so the GET composer can
    index every host field without a real remote runner."""
    from shared.config import get_config_metadata

    out: dict[str, Any] = {}
    for meta in get_config_metadata():
        if meta.scope != "host":
            continue
        if meta.name in overrides:
            out[meta.name] = overrides[meta.name]
        else:
            out[meta.name] = {
                "value": meta.default_value,
                "overridden": False,
                "remote_writable": meta.remote_writable,
                "can_enable": None,
                "reason": None,
            }
    return out


def test_get_unknown_machine_404() -> None:
    """GET ?machine=<unknown> (no row in machines) -> 404."""
    with TestClient(app) as client:
        resp = client.get("/api/config?machine=ghost-host")
    assert resp.status_code == 404, resp.text
    assert "unknown machine" in resp.json()["detail"]


def test_get_offline_machine_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET ?machine=<known-but-offline>: ops server unreachable -> 503."""
    _seed_machine(REMOTE)
    enqueue = AsyncMock(side_effect=_cluster_rpc.ClusterOpUnreachable("no ack"))
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.get(f"/api/config?machine={REMOTE}")
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_get_remote_failed_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET ?machine=<known>: the remote op fails (row marked failed) -> 503."""
    _seed_machine(REMOTE)
    enqueue = AsyncMock(side_effect=_cluster_rpc.ClusterOpFailed({"error": "boom"}))
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.get(f"/api/config?machine={REMOTE}")
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_get_local_unreachable_503_no_in_process_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LOCAL machine's ops server unreachable -> 503 like any other machine.
    The router has NO in-process fallback — a silent local read would mask the
    outage (the old behavior). This overrides the conftest's in-process stand-in
    for the local ops daemon."""
    from gateway.routers import config as config_router

    enqueue = AsyncMock(side_effect=_cluster_rpc.ClusterOpUnreachable("no ack"))
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.get("/api/config")  # machine absent -> the local machine
    assert resp.status_code == 503, resp.text
    assert "unreachable" in resp.json()["detail"]


# ── PUT ──


@pytest.fixture
def _clean_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect this gateway's .env (the single config store) to a fresh tmp file,
    so a PUT in these tests writes there instead of the real unit .env. The PUT no
    longer mutates the in-memory Settings (strict persist-and-signal), so there is
    nothing to save/restore."""
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    return tmp_path


def test_put_self_returns_write_result_and_routes_scopes(_clean_overrides: Path) -> None:
    """PUT self with a good cluster + host mix -> 200 + ConfigWriteResult,
    applied True, both stores written, restart_required is the union."""
    with TestClient(app) as client:
        resp = client.put(
            "/api/config",
            json={"llm_model": "put-cluster", "ops_concurrency": 4},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    # ops_concurrency (host) is the per-field result; cluster fields don't
    # appear (they can't fail a capability check).
    assert body["results"]["ops_concurrency"]["ok"] is True
    # restart_required = union of cluster llm_model ("agent") + host
    # ops_concurrency ("ops"), sorted.
    assert body["restart_required"] == ["agent", "ops"]

    # Both landed in this gateway's .env (the single store): cluster llm_model +
    # host ops_concurrency.
    assert runtime_config.env_set_field_names() == {"llm_model", "ops_concurrency"}
    aliases = runtime_config.read_env_aliases()
    assert aliases["AVA_MODEL"] == "put-cluster"
    assert aliases["AVA_OPS_CONCURRENCY"] == "4"


def test_put_self_capability_failure_writes_nothing(_clean_overrides: Path) -> None:
    """PUT self where a host field fails the capability validator -> applied
    False, results[field].ok False with a reason, and NOTHING written locally."""
    with TestClient(app) as client:
        # ops_concurrency=65 is out of the [1, 64] range the host validator
        # enforces -> the write is rejected atomically.
        resp = client.put("/api/config", json={"ops_concurrency": 65})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["results"]["ops_concurrency"]["ok"] is False
    assert body["results"]["ops_concurrency"]["reason"]
    # atomic: nothing landed in .env
    assert runtime_config.read_env_aliases() == {}


def test_put_rejects_agent_scope_field(_clean_overrides: Path) -> None:
    """An agent-scope field is writable=False, so the writable gate rejects it.

    sdk_disable has scope="agent" and writable=False; a PUT carrying it is
    rejected as "unknown or read-only" — same path as any other read-only field.
    Nothing is written to either store.
    """
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"sdk_disable": ["monitor"]})
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]
    # nothing written to .env
    assert runtime_config.read_env_aliases() == {}


def test_put_rejects_unknown_field(_clean_overrides: Path) -> None:
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"not_a_field": 1})
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]


def test_put_accepts_valid_enum_value(_clean_overrides: Path) -> None:
    """A cluster enum field accepts a value inside its choices and lands in .env."""
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"agent_reply_reminder_cadence": "every_time"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert runtime_config.read_env_aliases()["AVA_AGENT_REPLY_REMINDER_CADENCE"] == "every_time"


def test_put_rejects_invalid_enum_value(_clean_overrides: Path) -> None:
    """A value outside the enum's choices 400s atomically (nothing written), so an
    unknown enum can never reach .env and blow up Settings at the next restart."""
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"agent_reply_reminder_cadence": "sometimes"})
    assert resp.status_code == 400, resp.text
    assert "invalid enum value" in resp.json()["detail"]
    assert runtime_config.read_env_aliases() == {}


def test_get_self_sensitive_raw_override_is_sentinel(_clean_overrides: Path) -> None:
    """A sensitive cluster field set in .env appears in raw_overrides as the
    unchanged-sentinel, never its cleartext (no secret on the wire)."""
    from shared.config import CONFIG_UNCHANGED_SENTINEL

    runtime_config.write_fields({"deepseek_api_key": "sk-REAL-SECRET"}, set())
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    assert resp.json()["raw_overrides"]["deepseek_api_key"] == CONFIG_UNCHANGED_SENTINEL
    assert "sk-REAL-SECRET" not in resp.text


def test_put_sentinel_preserves_secret(_clean_overrides: Path) -> None:
    """Round-tripping the sentinel for an untouched secret keeps its .env value;
    a real edit to another field still lands."""
    from shared.config import CONFIG_UNCHANGED_SENTINEL

    runtime_config.write_fields({"deepseek_api_key": "sk-REAL-SECRET"}, set())
    with TestClient(app) as client:
        resp = client.put(
            "/api/config",
            json={"deepseek_api_key": CONFIG_UNCHANGED_SENTINEL, "llm_model": "m2"},
        )
    assert resp.status_code == 200, resp.text
    aliases = runtime_config.read_env_aliases()
    assert aliases["DEEPSEEK_API_KEY"] == "sk-REAL-SECRET"  # untouched
    assert aliases["AVA_MODEL"] == "m2"


def test_put_absent_key_preserves_secret(_clean_overrides: Path) -> None:
    """Reducer semantics: a PUT that does NOT mention a secret leaves it in place.

    This is the exact incident that once wiped a cluster's keys — a single-field
    PUT (here just llm_model) must not drop the DEEPSEEK/OPENAI keys it never
    named. Under the old full-replace, any managed key absent from the body was
    unset; under merge semantics an absent key is untouched."""
    runtime_config.write_fields(
        {"deepseek_api_key": "sk-REAL-SECRET", "openai_api_key": "sk-OPENAI"}, set()
    )
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"llm_model": "m2"})
    assert resp.status_code == 200, resp.text
    aliases = runtime_config.read_env_aliases()
    assert aliases["DEEPSEEK_API_KEY"] == "sk-REAL-SECRET"  # untouched (absent from body)
    assert aliases["OPENAI_API_KEY"] == "sk-OPENAI"  # untouched (absent from body)
    assert aliases["AVA_MODEL"] == "m2"


def test_put_replaces_one_secret_leaves_others(_clean_overrides: Path) -> None:
    """The API-key replace flow: the frontend PUTs a real new value for the ONE
    secret being replaced and the unchanged-sentinel for every other set secret
    (that is what raw_overrides carries). The named secret is rewritten; the others
    keep their .env value untouched — changing one key never disturbs another.

    Locks the isolation guarantee behind the Control page's write-only API-key
    replace: a new DEEPSEEK value lands while OPENAI (sentinel) and a non-secret
    field are preserved."""
    from shared.config import CONFIG_UNCHANGED_SENTINEL

    runtime_config.write_fields(
        {
            "deepseek_api_key": "sk-OLD-DEEPSEEK",
            "openai_api_key": "sk-OPENAI",
            "llm_model": "m1",
        },
        set(),
    )
    with TestClient(app) as client:
        resp = client.put(
            "/api/config",
            json={
                "deepseek_api_key": "sk-NEW-DEEPSEEK",  # the replaced key
                "openai_api_key": CONFIG_UNCHANGED_SENTINEL,  # untouched secret
                "llm_model": "m1",  # untouched non-secret (idempotent)
            },
        )
    assert resp.status_code == 200, resp.text
    aliases = runtime_config.read_env_aliases()
    assert aliases["DEEPSEEK_API_KEY"] == "sk-NEW-DEEPSEEK"  # rewritten
    assert aliases["OPENAI_API_KEY"] == "sk-OPENAI"  # untouched (sentinel)
    assert aliases["AVA_MODEL"] == "m1"


def test_put_null_unsets_cluster_field(_clean_overrides: Path) -> None:
    """Reducer semantics: a cluster field mapped to null is unset (reverted to its
    default); other set fields are untouched. Deletion is explicit, never by
    omission."""
    runtime_config.write_fields({"llm_model": "m1", "deepseek_api_key": "sk-X"}, set())
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"llm_model": None})
    assert resp.status_code == 200, resp.text
    aliases = runtime_config.read_env_aliases()
    assert "AVA_MODEL" not in aliases  # explicitly unset
    assert aliases["DEEPSEEK_API_KEY"] == "sk-X"  # untouched


def test_put_host_failure_leaves_cluster_unwritten(_clean_overrides: Path) -> None:
    """Atomic across scopes: a rejected host field means the cluster .env is not
    touched (host is written/validated first)."""
    with TestClient(app) as client:
        resp = client.put(
            "/api/config",
            json={"llm_model": "should-not-persist", "ops_concurrency": 65},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is False
    assert "AVA_MODEL" not in runtime_config.read_env_aliases()


def test_put_rejects_bad_scalar(_clean_overrides: Path) -> None:
    """A non-numeric value for a cluster int/float field 400s before any write,
    rather than landing in .env and crashing Settings at the next restart."""
    from shared.config import get_config_metadata

    scalar = next(
        m
        for m in get_config_metadata()
        if m.scope != "host" and m.writable and m.field_type in ("int", "float")
    )
    with TestClient(app) as client:
        resp = client.put("/api/config", json={scalar.name: "not-a-number"})
    assert resp.status_code == 400, resp.text
    assert scalar.name in resp.json()["detail"]
    assert runtime_config.read_env_aliases() == {}


def test_put_rejects_bool_for_int_field(_clean_overrides: Path) -> None:
    """A JSON bool for an int field is rejected (it would otherwise pass int() and
    persist as 'true', breaking Settings at the next restart)."""
    from shared.config import get_config_metadata

    scalar = next(
        m
        for m in get_config_metadata()
        if m.scope != "host" and m.writable and m.field_type == "int"
    )
    with TestClient(app) as client:
        resp = client.put("/api/config", json={scalar.name: True})
    assert resp.status_code == 400, resp.text
    assert runtime_config.read_env_aliases() == {}


def test_put_nonsensitive_field_can_hold_literal_sentinel(_clean_overrides: Path) -> None:
    """The sentinel-skip is gated on `sensitive`, so a non-sensitive field can be
    set to the literal sentinel string (no silent drop)."""
    from shared.config import CONFIG_UNCHANGED_SENTINEL

    with TestClient(app) as client:
        resp = client.put("/api/config", json={"llm_model": CONFIG_UNCHANGED_SENTINEL})
    assert resp.status_code == 200, resp.text
    assert runtime_config.read_env_aliases()["AVA_MODEL"] == CONFIG_UNCHANGED_SENTINEL


def test_put_self_edits_writable_host_capability_field(
    _clean_overrides: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writable host capability field (cross_machine_transfer_backend, not
    remote_writable) is editable on the self/Cluster view — the local write honors
    `writable`."""
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _f, _v: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"cross_machine_transfer_backend": "none"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert runtime_config.read_env_aliases()["AVA_CROSS_MACHINE_TRANSFER_BACKEND"] == "none"


@pytest.mark.asyncio
async def test_put_self_host_field_skips_empty_cluster_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host-only self PUT must not append an empty gateway cluster record."""
    host_result = ConfigWriteOpResult(
        machine=machine_name(),
        results={"cross_machine_transfer_backend": FieldWriteResult(ok=True)},
        applied=True,
        restart_required=[],
    )
    dispatch = AsyncMock(return_value=host_result)
    writes: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _write_fields(*args: object, **kwargs: object) -> None:
        writes.append((args, kwargs))

    def _assert_machine_known(_machine: str) -> None:
        return None

    monkeypatch.setattr(config_router, "_assert_machine_known", _assert_machine_known)
    monkeypatch.setattr(config_router, "_dispatch_config_write", dispatch)
    monkeypatch.setattr(config_router.runtime_config, "write_fields", _write_fields)

    result = await config_router.put_config({"cross_machine_transfer_backend": "none"})

    assert result.applied is True
    assert writes == []


def test_put_self_rejects_readonly_identity_field(_clean_overrides: Path) -> None:
    """An identity / connection field (machine_name) is read-only now — rejected by
    the writable gate, same as any other read-only field; nothing written."""
    with TestClient(app) as client:
        resp = client.put("/api/config", json={"machine_name": "renamed"})
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]
    assert "AVA_MACHINE_NAME" not in runtime_config.read_env_aliases()


def test_get_self_raw_overrides_exposes_writable_host_field_not_readonly(
    _clean_overrides: Path,
) -> None:
    """Self GET raw_overrides includes a writable host capability field (editable on
    the host's own panel) but excludes a now-read-only identity field, even when both
    are set in .env."""
    runtime_config.write_fields(
        {"cross_machine_transfer_backend": "none", "machine_name": "m"}, set()
    )
    with TestClient(app) as client:
        resp = client.get("/api/config")
    assert resp.status_code == 200, resp.text
    raw = resp.json()["raw_overrides"]
    assert raw.get("cross_machine_transfer_backend") == "none"  # writable -> self-editable
    assert "machine_name" not in raw  # writable=False -> read-only, not in the edit set


def test_put_explicit_self_edits_remote_writable_host_toggle(
    _clean_overrides: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a dual-role box, `?machine=<self>` is a host view (remote_writable gate),
    NOT the Cluster view — so a gateway-capability host toggle that is
    writable=False + remote_writable=True (heartbeat_enabled) is editable there,
    while the Cluster view (machine omitted) rejects it as read-only. Regression:
    the two used to collapse because both resolve target to machine_name(), leaving
    heartbeat_enabled / task_maintenance_enabled uneditable on a single box."""
    monkeypatch.setattr(
        host_config_validators,
        "validate",
        lambda _f, _v: host_config_validators.ValidationResult(ok=True),  # pyright: ignore[reportUnknownArgumentType]
    )
    with TestClient(app) as client:
        # Cluster view (machine omitted): writable=False -> rejected read-only.
        cluster_resp = client.put("/api/config", json={"heartbeat_enabled": False})
        # Explicit self host view: remote_writable=True -> accepted + written.
        host_resp = client.put(
            f"/api/config?machine={machine_name()}", json={"heartbeat_enabled": False}
        )
    assert cluster_resp.status_code == 400
    assert "unknown or read-only" in cluster_resp.json()["detail"]
    assert host_resp.status_code == 200, host_resp.text
    assert host_resp.json()["applied"] is True
    assert runtime_config.read_env_aliases()["AVA_HEARTBEAT_ENABLED"] == "false"


def test_get_explicit_self_is_host_view_not_cluster(_clean_overrides: Path) -> None:
    """GET `?machine=<self>` is a host view: raw_overrides is the remote_writable
    host set, so a writable-but-not-remote_writable field
    (cross_machine_transfer_backend) that the Cluster view exposes is ABSENT here —
    the explicit self selection no longer collapses into the Cluster view."""
    runtime_config.write_fields({"cross_machine_transfer_backend": "none"}, set())
    with TestClient(app) as client:
        cluster = client.get("/api/config").json()["raw_overrides"]
        host = client.get(f"/api/config?machine={machine_name()}").json()["raw_overrides"]
    assert (
        cluster.get("cross_machine_transfer_backend") == "none"
    )  # writable -> Cluster view exposes it
    assert (
        "cross_machine_transfer_backend" not in host
    )  # not remote_writable -> host view excludes it


# ── PUT remote ──


def test_put_remote_rejects_cluster_key(_clean_overrides: Path) -> None:
    """PUT ?machine=<remote> carrying a cluster key -> 400 (cluster config is
    machine-independent)."""
    _seed_machine(REMOTE)
    with TestClient(app) as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"llm_model": "x"})
    assert resp.status_code == 400, resp.text
    assert "machine-independent" in resp.json()["detail"]


def test_put_remote_host_field_dispatches_config_write(
    monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path
) -> None:
    """PUT ?machine=<remote> with a host remote_writable key -> dispatches a
    config_write with payload {"overrides": {...}} and returns the stubbed
    ConfigWriteResult."""
    _seed_machine(REMOTE)
    stub_result = {
        "machine": REMOTE,
        "results": {"ops_concurrency": {"ok": True, "reason": None}},
        "applied": True,
        "restart_required": ["ops"],
    }
    enqueue = AsyncMock(return_value=stub_result)
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"ops_concurrency": 12})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["results"]["ops_concurrency"]["ok"] is True
    assert body["restart_required"] == ["ops"]

    assert enqueue.await_count == 1
    assert enqueue.await_args is not None
    kwargs = enqueue.await_args.kwargs
    assert kwargs["target_machine"] == REMOTE
    assert kwargs["kind"] == "config_write"
    assert kwargs["payload"] == {"overrides": {"ops_concurrency": 12}, "local": False}


def test_put_remote_rejects_writable_non_remote_host_field(
    monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path
) -> None:
    """A writable-but-not-remote_writable host field (cross_machine_transfer_backend) is
    rejected at the gate on a machine-addressed PUT — 400 before any dispatch,
    rather than a 200 applied=False from the host-side op. Locks the shared
    field_editable gate: remote host edits require remote_writable."""
    _seed_machine(REMOTE)
    dispatched = AsyncMock()
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", dispatched)
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", dispatched)
    with TestClient(app) as client:
        resp = client.put(
            f"/api/config?machine={REMOTE}", json={"cross_machine_transfer_backend": "none"}
        )
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]
    dispatched.assert_not_awaited()


def test_put_remote_rejects_oss_credential_fields(
    monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path
) -> None:
    """OSS credential paths are host-local editable only: a machine-addressed PUT
    400s at the gate (remote_writable=False), so a remote admin can never point a
    machine at arbitrary credential files. Locks the security boundary the
    writable flip must not loosen."""
    _seed_machine(REMOTE)
    dispatched = AsyncMock()
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", dispatched)
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", dispatched)
    with TestClient(app) as client:
        for field in ("pitr_oss_credentials_file", "pitr_oss_viewer_credentials_file"):
            resp = client.put(
                f"/api/config?machine={REMOTE}",
                json={field: str(_clean_overrides / "attacker-credentials.json")},
            )
            assert resp.status_code == 400, resp.text
            assert "unknown or read-only" in resp.json()["detail"]
    dispatched.assert_not_awaited()


def test_put_remote_offline_503(monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path) -> None:
    """PUT ?machine=<known-but-offline>: config_write times out -> 503."""
    _seed_machine(REMOTE)
    enqueue = AsyncMock(side_effect=_cluster_rpc.ClusterOpUnreachable("no ack"))
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"ops_concurrency": 12})
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_put_remote_failed_503(monkeypatch: pytest.MonkeyPatch, _clean_overrides: Path) -> None:
    """PUT ?machine=<known>: the remote config_write fails (row marked failed) -> 503."""
    _seed_machine(REMOTE)
    enqueue = AsyncMock(side_effect=_cluster_rpc.ClusterOpFailed({"error": "boom"}))
    monkeypatch.setattr(_cluster_rpc, "dispatch_to_machine", enqueue)
    monkeypatch.setattr(config_router._cluster_rpc, "dispatch_to_machine", enqueue)

    with TestClient(app) as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"ops_concurrency": 12})
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_put_remote_unknown_machine_404(_clean_overrides: Path) -> None:
    """PUT ?machine=<unknown> -> 404 before any dispatch."""
    with TestClient(app) as client:
        resp = client.put("/api/config?machine=ghost-host", json={"ops_concurrency": 1})
    assert resp.status_code == 404, resp.text
    assert "unknown machine" in resp.json()["detail"]


def test_self_machine_name_is_known_without_a_row() -> None:
    """Sanity: the resolved self machine_name is treated as known even with no
    machines row (the GET/PUT self path must not 404)."""
    # machine_name() resolves to this process's identity; _assert_machine_known
    # short-circuits it. A bare self GET already exercises this, but assert the
    # name is non-empty so the short-circuit is meaningful.
    assert machine_name()
