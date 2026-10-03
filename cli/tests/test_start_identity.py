"""`ava init` records one home identity before effects; `ava start` admits it."""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from dotenv import dotenv_values

from base import cluster
from base.native_process.os_platform import file_lock
from cli import init_intent, start_intent
from cli import start_identity as identity
from cli.tests._init_identity import prepare_init_identity


@pytest.fixture(autouse=True)
def _isolate_start_environment() -> Iterator[None]:
    """Real start clears derived keys; that process-local effect ends with its test."""
    with patch.dict(os.environ):
        yield


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> identity.IdentityInput:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(cluster, "port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    return identity.IdentityInput(
        tmp_path / "home",
        checkout,
        frozenset({"gateway", "agent-runner"}),
        {"AVA_MACHINE_NAME": "preview"},
    )


def test_fresh_identity_committed_before_native_work(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    data = identity.read_intent(inputs.home)
    assert data is not None and data["phase"] == "configured"
    rec = cluster.get_record(inputs.home)
    env = dotenv_values(inputs.home / ".env")
    assert rec is not None and "pgbouncer" in rec.ports
    db_url = env["AVA_DB_URL"]
    assert db_url is not None and db_url.endswith(f":{rec.ports['pgbouncer']}/ava")
    # The database endpoint is credential-free: the owner is NOLOGIN and the
    # first start mints write generation 0; no DB password is ever minted here.
    assert urlsplit(db_url).password is None
    assert env["AVA_CLUSTER_SECRET"] == ""
    assert "AVA_DB_ADMIN_PASSWORD" not in env
    assert "AVA_RUNNER_DB_PASSWORD" not in env
    # Redis always authenticates: a no-secret single box is still born with
    # independent Redis admin and runtime passwords, the runtime one in its URL.
    redis_admin, redis_runtime = env["AVA_REDIS_ADMIN_PASSWORD"], env["AVA_REDIS_PASSWORD"]
    assert redis_admin and redis_runtime and redis_admin != redis_runtime
    redis_url = urlsplit(env["AVA_REDIS_URL"] or "")
    assert (redis_url.username, redis_url.password) == ("ava", redis_runtime)
    assert not (inputs.home / "pg").exists()


def test_an_initialized_home_is_refused_again_and_keeps_its_bytes(
    inputs: identity.IdentityInput,
) -> None:
    inputs = replace(inputs, roles=frozenset({"gateway"}))
    identity.prepare_identity(inputs)
    paths = [inputs.home / ".env", inputs.home / identity.INTENT_NAME]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    env = dotenv_values(paths[0])
    keys = ("AVA_CLUSTER_SECRET", "AVA_REDIS_ADMIN_PASSWORD", "AVA_REDIS_PASSWORD")
    assert len({env[k] for k in keys}) == len(keys)
    assert all(env[k] for k in keys)
    for phase in ("configured", "provisioned", "ready"):
        identity.mark_phase(inputs.home, phase)
        with pytest.raises(RuntimeError, match=rf"already initialized \(phase {phase}\)"):
            identity.prepare_identity(inputs)
        with pytest.raises(RuntimeError, match="already initialized"):
            identity.resume_claim(inputs.home)
        if phase == "configured":
            assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before
    assert dotenv_values(paths[0]) == env


def test_a_gateway_birth_pins_a_minted_logical_backup_passphrase(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logical backups are encrypted under a passphrase the birth mints and pins
    (0600), independent of the cluster secret: an empty-secret single box gets
    a random one, never the public sha256(""). An interrupted birth keeps it."""
    from services.gateway_side.backup import passphrase

    upsert = identity.upsert_env

    def crash(*_a: object, **_k: object) -> None:
        raise OSError("power loss")

    monkeypatch.setattr(identity, "upsert_env", crash)
    with pytest.raises(OSError, match="power loss"):
        identity.prepare_identity(inputs)
    minted = passphrase.pinned(inputs.home)
    assert minted is not None and minted != passphrase.LEGACY_EMPTY_SECRET_PASSPHRASE
    assert passphrase.pin_path(inputs.home).stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(identity, "upsert_env", upsert)
    identity.resume_claim(inputs.home)
    assert dotenv_values(inputs.home / ".env")["AVA_CLUSTER_SECRET"] == ""
    assert passphrase.resolve(inputs.home) == minted


def test_a_remote_unit_birth_pins_no_backup_passphrase(inputs: identity.IdentityInput) -> None:
    """Only a gateway runs logical backups; a joining agent-runner holds no key."""
    from services.gateway_side.backup import passphrase

    runner = replace(
        inputs,
        roles=frozenset({"agent-runner"}),
        values={**inputs.values, "AVA_GATEWAY_URL": "http://10.0.0.7:8000"},
    )
    identity.prepare_identity(runner)
    assert passphrase.pinned(runner.home) is None


def test_a_published_claim_keeps_no_copy_of_its_credentials(
    inputs: identity.IdentityInput,
) -> None:
    """The intent carries the birth's `.env` payload only while it is claiming,
    so an interrupted birth resumes the same credentials. Once `.env` holds
    them the intent drops the payload: no stale copy of the human secret or the
    Redis passwords outlives a rotation there."""
    inputs = replace(inputs, roles=frozenset({"gateway"}))
    identity.prepare_identity(inputs)
    env = dotenv_values(inputs.home / ".env")
    credentials = [
        env[key] for key in ("AVA_CLUSTER_SECRET", "AVA_REDIS_ADMIN_PASSWORD", "AVA_REDIS_PASSWORD")
    ]
    assert all(credentials)
    data = identity.read_intent(inputs.home)
    assert data is not None and data["phase"] == "configured" and data["env"] == {}
    raw = (inputs.home / identity.INTENT_NAME).read_text()
    assert not [value for value in credentials if value and value in raw]


def test_birth_writes_nothing_outside_the_home(inputs: identity.IdentityInput) -> None:
    """A home describes only itself: no host-level file and no file in the
    checkout lists the cluster."""
    identity.prepare_identity(inputs)
    assert sorted(p.name for p in inputs.home.parent.iterdir()) == ["checkout", "home"]
    assert list(inputs.checkout.iterdir()) == []


def test_crash_after_intent_before_env_resumes_same_claim(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = identity.upsert_env
    monkeypatch.setattr(
        identity,
        "upsert_env",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("power loss")),  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    with pytest.raises(OSError, match="power loss"):
        identity.prepare_identity(inputs)
    pending = identity.read_intent(inputs.home)
    assert pending is not None and pending["phase"] == "claiming"
    monkeypatch.setattr(identity, "upsert_env", write)
    identity.resume_claim(inputs.home)
    assert cluster.get_record(inputs.home) == cluster.ClusterRecord(**pending["record"])
    assert dict(dotenv_values(inputs.home / ".env")) == pending["env"]


def test_an_intent_carrying_the_retired_worktree_key_is_refused_by_name(
    inputs: identity.IdentityInput,
) -> None:
    """The `worktree` flag an older start recorded is not read, migrated or
    ignored: a home whose intent still carries it fails loudly at its next start,
    naming the key, until the operator removes it."""
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["worktree"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match=r"unexpected keys \[.*'worktree'"):
        identity.read_intent(inputs.home)


# The one-line rollout step for a home born before that key was retired. It is
# quoted verbatim in the pull request that retires it.
_STRIP_RETIRED_INTENT_KEY = (
    "python3 -c 'import json,os,sys;p=sys.argv[1];d=json.load(open(p));"
    'd.pop("worktree",None);t=p+".tmp";'
    "f=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.fchmod(f,0o600);"
    'os.write(f,(json.dumps(d,sort_keys=True)+"\\n").encode());os.fsync(f);os.close(f);'
    "os.replace(t,p)' "
)


def test_the_rollout_step_makes_an_old_intent_readable_idempotently(
    inputs: identity.IdentityInput,
) -> None:
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    born_before = json.loads(path.read_text()) | {"worktree": True}
    path.write_text(json.dumps(born_before, sort_keys=True) + "\n")
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="unexpected keys"):
        identity.read_intent(inputs.home)

    command = _STRIP_RETIRED_INTENT_KEY + f"'{path}'"
    for _ in range(2):  # the second run changes nothing
        subprocess.run(command, shell=True, check=True, capture_output=True)  # noqa: S602 — the quoted rollout line
        assert path.stat().st_mode & 0o777 == 0o600
        assert not path.with_name(path.name + ".tmp").exists()
        data = identity.read_intent(inputs.home)
        assert data == {k: v for k, v in born_before.items() if k != "worktree"}


def _runbook_port_slot_step() -> str:
    """The python snippet the runbook gives for dropping the retired port slots."""
    runbook = (Path(__file__).resolve().parents[2] / "conventions" / "runbook.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)\n\s*```", runbook, flags=re.DOTALL)
    (step,) = [b for b in blocks if "start-intent.json" in b and "pitr_uploader" in b]
    return textwrap.dedent(step)


def test_the_runbook_step_drops_the_retired_port_slots_idempotently(
    inputs: identity.IdentityInput,
) -> None:
    """A home born with the PITR slots and the two watchdog slots is refused by new
    code, every one of them named, until the runbook's rollout step has run; the
    step, run verbatim from the runbook, makes the intent readable again, leaves it
    private and does nothing the second time."""
    retired = {
        "pitr_uploader": 8117,
        "pitr_base_backup": 8118,
        "gateway_watchdog": 8119,
        "agent_runner_watchdog": 8120,
    }
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["record"]["ports"] |= retired
    path.write_text(json.dumps(data, sort_keys=True) + "\n")
    path.chmod(0o600)
    with pytest.raises(
        RuntimeError,
        match=r"unexpected \['agent_runner_watchdog', 'gateway_watchdog', "
        r"'pitr_base_backup', 'pitr_uploader'\]",
    ):
        identity.read_intent(inputs.home)

    for _ in range(2):  # the second run changes nothing
        subprocess.run(  # noqa: S603 — the runbook's own snippet
            ["bash", "-c", _runbook_port_slot_step()],
            env={"PATH": os.environ["PATH"], "AVA_HOME": str(inputs.home)},
            check=True,
            capture_output=True,
        )
        assert path.stat().st_mode & 0o777 == 0o600
        assert not list(inputs.home.glob("start-intent.json.*"))
        read = identity.read_intent(inputs.home)
        assert read is not None and read["record"]["ports"] == cluster.get_record(inputs.home).ports  # pyright: ignore[reportOptionalMemberAccess]
        assert not set(retired) & set(read["record"]["ports"])


def test_the_runbook_step_drops_the_milvus_port_slot_idempotently(
    inputs: identity.IdentityInput,
) -> None:
    """A gateway home born before the milvus slot was retired is refused by new code,
    the slot named, until the runbook's rollout step has run; the step, run verbatim
    from the runbook, makes the intent readable again, leaves it private and does
    nothing the second time."""
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["record"]["ports"]["milvus"] = 19530
    path.write_text(json.dumps(data, sort_keys=True) + "\n")
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match=r"unexpected \['milvus'\]"):
        identity.read_intent(inputs.home)

    runbook = (Path(__file__).resolve().parents[2] / "conventions" / "runbook.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)\n\s*```", runbook, flags=re.DOTALL)
    (step,) = [b for b in blocks if "start-intent.json" in b and 'pop("milvus"' in b]
    for _ in range(2):  # the second run changes nothing
        subprocess.run(  # noqa: S603 — the runbook's own snippet
            ["bash", "-c", textwrap.dedent(step)],
            env={"PATH": os.environ["PATH"], "AVA_HOME": str(inputs.home)},
            check=True,
            capture_output=True,
        )
        assert path.stat().st_mode & 0o777 == 0o600
        assert not list(inputs.home.glob("start-intent.json.*"))
        read = identity.read_intent(inputs.home)
        assert read is not None and "milvus" not in read["record"]["ports"]


def test_configured_home_missing_its_intent_is_not_reborn(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    (inputs.home / identity.INTENT_NAME).unlink()
    with pytest.raises(RuntimeError, match="reattachment"):
        identity.prepare_identity(inputs)
    assert identity.read_intent(inputs.home) is None


def test_existing_data_without_intent_refuses(inputs: identity.IdentityInput) -> None:
    (inputs.home / "pg").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="no initialization authority"):
        identity.prepare_identity(inputs)
    assert identity.read_intent(inputs.home) is None


def _args(*extra: str):
    from cli.parsers import build_parser

    return build_parser().parse_args(["init", *extra])


def _start_args(*extra: str):
    from cli.parsers import build_parser

    return build_parser().parse_args(["start", *extra])


def _single_box(*extra: str):
    """An init of a single-box home: gateway and agent-runner, named."""
    return _args("--serve-gateway", "--serve-agent-runner", "--machine-name", "preview", *extra)


def test_init_uses_explicit_home_not_ambient_projection(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.setenv("AVA_DB_URL", "postgresql://foreign@foreign.invalid/production")
    monkeypatch.setenv("AVA_CLUSTER_SECRET", "foreign-secret")
    prepare_init_identity(_single_box())
    env = dotenv_values(inputs.home / ".env")
    db_url = env["AVA_DB_URL"]
    assert db_url is not None and "foreign" not in db_url
    assert env["AVA_CLUSTER_SECRET"] == ""


def test_interrupted_init_keeps_admitted_tool_path(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.delenv("AVA_SERVICE_PATH", raising=False)
    activated = inputs.checkout.parent / "other-venv"
    tools = str(inputs.checkout.parent / "tools")
    bin_name = "bin"
    monkeypatch.setenv("VIRTUAL_ENV", str(activated))
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            (str(activated / bin_name), str(inputs.checkout / ".venv" / bin_name), tools)
        ),
    )
    write = identity.upsert_env

    def crash(*_args: object, **_kwargs: object) -> None:
        raise OSError("power loss")

    monkeypatch.setattr(identity, "upsert_env", crash)
    with pytest.raises(OSError, match="power loss"):
        prepare_init_identity(_single_box())
    pending = identity.read_intent(inputs.home)
    assert pending is not None and pending["phase"] == "claiming"
    assert pending["env"]["AVA_SERVICE_PATH"] == tools
    monkeypatch.setattr(identity, "upsert_env", write)
    monkeypatch.setenv("PATH", str(inputs.checkout.parent / "different-tools"))
    prepare_init_identity(_args())  # the resume takes no flags
    assert dotenv_values(inputs.home / ".env")["AVA_SERVICE_PATH"] == tools


def test_existing_home_cannot_recapture_caller_tool_path(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity.prepare_identity(inputs)
    monkeypatch.setenv("AVA_SERVICE_PATH", str(inputs.checkout.parent / "unadmitted"))
    with pytest.raises(ValueError, match="explicit AVA_SERVICE_PATH"):
        init_intent._service_path(identity.stored_values(inputs.home), inputs.home)


def test_lossy_tool_path_is_rejected_before_identity_publication(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    monkeypatch.delenv("AVA_SERVICE_PATH", raising=False)
    monkeypatch.setenv("PATH", str(inputs.checkout.parent / "tools #1"))
    with pytest.raises(ValueError, match="round-trip literally"):
        prepare_init_identity(_single_box())
    assert identity.read_intent(inputs.home) is None


@pytest.mark.parametrize(
    "key", ["AVA_HOME", "AVA_GATEWAY_PORT", "AVA_DB_URL", "AVA_MACHINE_NAME", "UNKNOWN_SETTING"]
)
def test_config_file_cannot_choose_identity(inputs: identity.IdentityInput, key: str) -> None:
    config = inputs.home.parent / "input.env"
    config.write_text(f"{key}=wrong\n")
    with pytest.raises(ValueError, match="config file"):
        init_intent._config_values(_args("--config-file", str(config)), inputs.home)


_RUNNER_ENDPOINT = "postgresql://ava@remote.invalid/db"


_FETCH_BEARERS: list[object] = []


def _runner_start(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, *extra: str
) -> Any:
    """A remote runner's first start against a gateway serving the endpoint only."""
    from base.host.env import bootstrap

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    # A remote unit joins without the human secret: its capability authenticates.
    monkeypatch.delenv("AVA_CLUSTER_SECRET", raising=False)

    def served(*_a: object, **kwargs: object) -> dict[str, str]:
        _FETCH_BEARERS.append(kwargs.get("bearer"))
        return {"AVA_DB_URL": _RUNNER_ENDPOINT, "AVA_REDIS_URL": "redis://remote.invalid/0"}

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", served)
    return _args(
        "--serve-agent-runner",
        "--no-serve-gateway",
        "--machine-name",
        "runner",
        "--machine-host",
        "runner.invalid",
        "--gateway-url",
        "https://gateway.invalid",
        *extra,
    )


def test_runner_without_a_capability_refuses_before_persisting(
    inputs: identity.IdentityInput,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seed_write_generation: Any,
) -> None:
    """Bootstrap serves no login, so a runner's first start needs its unit
    capability; without one it refuses before any identity is written."""
    from base.cluster.authority import unit

    args = _runner_start(inputs, monkeypatch)
    with pytest.raises(ValueError, match="holds no database capability"):
        prepare_init_identity(args)
    assert not (inputs.home / ".env").exists()
    # An installed capability (a bundle the operator carried earlier) admits it.
    gateway = tmp_path / "gateway"
    gateway.mkdir(mode=0o700)
    seed_write_generation(gateway)
    home = inputs.home.resolve()
    issued = unit.issue_bundle(
        gateway.resolve(),
        unit=unit.UnitIdentity(machine="runner", home=str(home)),
        endpoint=_RUNNER_ENDPOINT,
        cluster_secret="gateway-human-secret-" + "g" * 32,
        ttl_s=60,
    )
    unit.install_bundle(
        home,
        unit.open_bundle(issued.envelope, issued.transport_key),
        machine="runner",
        served_endpoint=_RUNNER_ENDPOINT,
        probe=lambda _dsn: None,
    )
    _FETCH_BEARERS.clear()
    prepare_init_identity(args)
    env = dotenv_values(inputs.home / ".env")
    assert env["AVA_MACHINE_HOST"] == "runner.invalid"
    assert "AVA_DB_URL" not in env and "AVA_REDIS_URL" not in env
    # The fetch authenticated with the capability's API token; no bearer persisted.
    installed = unit.require_unit_capability(home)
    assert installed.api is not None and [installed.api.token] == _FETCH_BEARERS
    assert "AVA_CLUSTER_SECRET" not in env
    assert cluster.get_record(home) is None


def test_capability_bundle_needs_its_transport_key(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "unit.bundle"
    bundle.write_text("{}")
    monkeypatch.delenv("AVA_DB_CAPABILITY_KEY", raising=False)
    args = _runner_start(inputs, monkeypatch, "--db-capability", str(bundle))
    with pytest.raises(ValueError, match="AVA_DB_CAPABILITY_KEY"):
        prepare_init_identity(args)
    assert bundle.exists()
    assert not (inputs.home / ".env").exists()


def test_gateway_init_refuses_a_capability_bundle(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    with pytest.raises(ValueError, match="agent-runner units"):
        prepare_init_identity(_single_box("--db-capability", str(tmp_path / "unit.bundle")))
    assert identity.read_intent(inputs.home) is None


def test_ready_phase_never_regresses_or_rewrites(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    identity.mark_phase(inputs.home, "ready")
    path = inputs.home / identity.INTENT_NAME
    before = path.read_bytes(), path.stat().st_mtime_ns
    identity.mark_phase(inputs.home, "provisioned")
    identity.mark_phase(inputs.home, "ready")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_birth_records_the_ports_a_new_home_gets(
    inputs: identity.IdentityInput, session_ports: dict[str, int]
) -> None:
    """A test home records this session's own ports, never the fixed table's: the
    table is what the operator's cluster on the same box binds."""
    identity.prepare_identity(inputs)
    rec = cluster.get_record(inputs.home)
    assert rec is not None
    assert dict(rec.ports) == session_ports
    assert not set(session_ports.values()) & set(cluster.FIXED_PORTS.values())


def test_birth_refuses_ports_that_are_already_bound(
    inputs: identity.IdentityInput, session_ports: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cluster, "port_free", lambda port: port != session_ports["gateway"])  # pyright: ignore[reportUnknownArgumentType] — test double
    with pytest.raises(RuntimeError, match="already occupied"):
        identity.prepare_identity(inputs)
    assert not (inputs.home / identity.INTENT_NAME).exists()


def test_the_fixed_table_is_a_valid_reservation(inputs: identity.IdentityInput) -> None:
    """A production home records exactly the fixed table; its intent reads back
    unchanged."""
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["record"]["ports"] = dict(cluster.FIXED_PORTS)
    path.write_text(json.dumps(data))
    read = identity.read_intent(inputs.home)
    assert read is not None and read["record"]["ports"] == cluster.FIXED_PORTS


@pytest.mark.parametrize(
    ("slot", "present", "message"),
    [
        ("retired_slot", False, r"unexpected \['retired_slot'\]"),
        ("memory_search", True, r"missing \['memory_search'\]"),
    ],
)
def test_reservation_must_match_the_fixed_table_exactly(
    inputs: identity.IdentityInput, slot: str, present: bool, message: str
) -> None:
    """A record carrying a slot the table lacks (or lacking one it has) is refused
    at once and names the difference, so a hand step left undone fails the start
    loudly instead of running on a stale layout."""
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    if present:
        del data["record"]["ports"][slot]
    else:
        data["record"]["ports"][slot] = 8102
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match=rf"invalid start port reservation.*{message}"):
        identity.read_intent(inputs.home)


def test_corrupt_foreign_reservation_never_publishes(inputs: identity.IdentityInput) -> None:
    identity.prepare_identity(inputs)
    path = inputs.home / identity.INTENT_NAME
    data = json.loads(path.read_text())
    data["phase"] = "claiming"
    data["record"]["gateway_home"] = str(inputs.home.parent / "foreign")
    path.write_text(json.dumps(data))
    before = (inputs.home / ".env").read_bytes()
    with pytest.raises(RuntimeError, match="another home"):
        identity.resume_claim(inputs.home)
    assert (inputs.home / ".env").read_bytes() == before


def test_config_file_is_taken_once_and_a_second_init_is_refused(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    config = inputs.home.parent / "config.env"
    config.write_text("AVA_LLM_OVERRIDE=fixture:model\n")
    args = _single_box("--config-file", str(config))
    prepare_init_identity(args)
    first = (inputs.home / ".env").read_bytes()
    assert b"AVA_LLM_OVERRIDE" in first
    config.write_text("AVA_LLM_OVERRIDE=other:model\n")
    with pytest.raises(RuntimeError, match="already initialized"):
        prepare_init_identity(args)
    assert (inputs.home / ".env").read_bytes() == first
    # A home that already has an `.env` without an intent takes no config file either.
    with pytest.raises(ValueError, match=r"no `\.env` yet"):
        init_intent._config_values(args, inputs.home)


def _remote_config(
    inputs: identity.IdentityInput,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str = "db.invalid",
    query: str = "",
):
    import socket

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setattr(
        init_intent,
        "_local_addresses",
        lambda _machine: {"this-host", "192.0.2.1"},  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.55", 0))],  # pyright: ignore[reportUnknownArgumentType] — test double or third-party stubs
    )
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    config = inputs.home.parent / "remote.env"
    config.write_text(
        f"AVA_DB_URL=postgresql://owner:provider@{host}:6543/app{query}\nAVA_REDIS_URL=rediss://acl:provider@redis.invalid:6381/0\nAVA_RUNNER_DB_PASSWORD=external-runner\n"
    )
    return _single_box("--config-file", str(config))


def test_remote_init_preserves_provider_urls_and_runner_credential(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _remote_config(inputs, monkeypatch)
    prepare_init_identity(args)
    env = dotenv_values(inputs.home / ".env")
    assert env["AVA_DB_URL"] == "postgresql://owner:provider@db.invalid:6543/app"
    assert env["AVA_REDIS_URL"] == "rediss://acl:provider@redis.invalid:6381/0"
    assert env["AVA_RUNNER_DB_PASSWORD"] == "external-runner"  # noqa: S105 — fixture credential
    assert "AVA_DB_ADMIN_PASSWORD" not in env
    assert "AVA_REDIS_ADMIN_PASSWORD" not in env
    before = (inputs.home / ".env").read_bytes()
    with pytest.raises(RuntimeError, match="already initialized"):
        prepare_init_identity(args)
    assert (inputs.home / ".env").read_bytes() == before


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "this-host", "192.0.2.1"])
def test_remote_input_refuses_local_or_mixed_ownership(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    args = _remote_config(inputs, monkeypatch, host=host)
    with pytest.raises(ValueError, match="foreign"):
        prepare_init_identity(args)
    assert identity.read_intent(inputs.home) is None


@pytest.mark.parametrize("query", ["?hostaddr=127.0.0.1", "?host=", "?port=", "?dbname=other"])
def test_remote_input_cannot_redirect_libpq_identity(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    args = _remote_config(inputs, monkeypatch, query=query)
    with pytest.raises(ValueError, match="redirect"):
        prepare_init_identity(args)
    assert identity.read_intent(inputs.home) is None


def test_public_start_holds_home_lock_through_runtime_start(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:

    from base.native_process.os_platform import LockTimeoutError

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))

    def runtime(**_kw: object) -> int:
        with (
            pytest.raises(LockTimeoutError),
            file_lock(inputs.home / "start-intent.lock", timeout_s=0.05),
        ):
            pytest.fail("another lifecycle operation entered during start")
        return 0

    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", runtime)
    prepare_init_identity(_single_box())
    assert start_intent.run_start(_start_args()) == 0


def test_start_parser_rejects_retired_updater_telemetry() -> None:
    with pytest.raises(SystemExit) as error:
        _start_args("--updater-telemetry")
    assert error.value.code == 2


@pytest.mark.parametrize("result", [0, 1, 75])
def test_public_start_publishes_boot_pid_only_after_complete_success(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch, result: int
) -> None:

    from cli.commands.lifecycle import root_driver

    calls: list[str] = []
    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))

    def runtime(**_kw: object) -> int:
        calls.append("complete wrapped start")
        return result

    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", runtime)
    monkeypatch.setattr(root_driver, "complete_boot_start", lambda: calls.append("publish PID"))
    prepare_init_identity(_single_box())
    assert start_intent.run_start(_start_args()) == result
    assert calls == ["complete wrapped start"] + (["publish PID"] if result == 0 else [])


def test_failed_boot_publication_clears_serving_and_refuses_success(
    inputs: identity.IdentityInput, monkeypatch: pytest.MonkeyPatch
) -> None:

    from base.deploy.lifecycle import start_serving
    from cli.commands.lifecycle import root_driver

    monkeypatch.setattr(start_intent, "_checkout", lambda: inputs.checkout)
    monkeypatch.setenv("AVA_HOME", str(inputs.home))
    calls: list[str] = []

    def ready(**_kw: object) -> int:
        return 0

    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", ready)
    monkeypatch.setattr(start_serving, "clear_serving", lambda: calls.append("cleared"))

    def fail() -> None:
        raise RuntimeError("manager refused native custody")

    monkeypatch.setattr(root_driver, "complete_boot_start", fail)
    prepare_init_identity(_single_box())
    assert start_intent.run_start(_start_args()) == 1
    assert calls == ["cleared"]
