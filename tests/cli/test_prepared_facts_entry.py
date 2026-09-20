"""The prepared-facts entry's refusal contract and lock-scope discipline.

Pins the platform/runtime gates, the exit-2 + sanitized-line refusal contract
for every refusal class (filesystem, database, lease, native-read and
machine-role errors alike), and the F1 fix: collection/sealing run outside the
deployment lock transaction, which fences lease liveness and registration
only. The entry's deep data checks (schema baseline, lease liveness,
registration, receipt seal, selector) need a Linux + wheel runtime and stay
unpinned at this unit layer; they ride the process-level prove family
(scripts/prove_release_inventory.py, extended by the dispatch slices) in CI.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cli import prepared_facts
from shared.machine import MachineRoleMissing
from shared.managed_writer_barrier import ManagedWriterBarrierError
from shared.managed_writer_observation import ExpectedUnitWriters
from shared.managed_writer_publication import NormalService
from shared.native_job_observation import NativeReadUnavailableError
from shared.runtime_publication_input import PreparationReceipt, PreparedService
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease

ARTIFACT = "a" * 64
MANIFEST = "b" * 64
SCHEMA = "c" * 64
RECOVERY_ARTIFACT = "d" * 64
RECOVERY_MANIFEST = "e" * 64
RECOVERY_SCHEMA = "f" * 64

ARGV = [
    "--operation-holder",
    "gateway:pid1",
    "--operation-acquired-at",
    "2026-09-20T00:00:00+00:00",
    "--operation-target-sha",
    "0" * 40,
    "--artifact-digest",
    ARTIFACT,
    "--manifest-digest",
    MANIFEST,
    "--schema-digest",
    SCHEMA,
    "--recovery-artifact-digest",
    RECOVERY_ARTIFACT,
    "--recovery-manifest-digest",
    RECOVERY_MANIFEST,
    "--recovery-schema-digest",
    RECOVERY_SCHEMA,
]


def test_non_linux_platform_refuses_before_any_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q7: macOS/Windows units fail closed at preparation -- the restricted hop
    has no native proof there, and that refusal must not wait inside the hop."""
    monkeypatch.setattr(prepared_facts.sys, "platform", "darwin")
    args = prepared_facts._parser().parse_args(ARGV)
    with pytest.raises(ReleaseRejectedError, match="restricted hop supports"):
        prepared_facts.produce_facts(args)


def test_source_cannot_impersonate_the_candidate_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prepared_facts.sys, "platform", "linux")
    monkeypatch.setattr(prepared_facts, "WHEEL_RUNTIME", False)
    with pytest.raises(ReleaseRejectedError, match="verified candidate runtime"):
        prepared_facts._loaded_unit()


@pytest.mark.parametrize(
    "error",
    [
        ManagedWriterBarrierError("managed writer evidence does not own the current live rollout"),
        NativeReadUnavailableError("native launcher read unavailable"),
        MachineRoleMissing("this host serves no capability"),
    ],
)
def test_every_refusal_class_exits_two_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: RuntimeError
) -> None:
    """F2: the exit-2/sanitized-line refusal contract covers the RuntimeError
    refusals the chain actually raises (dead lease, native read, machine role)."""

    def refuse(_args: Any) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(prepared_facts, "produce_facts", refuse)

    assert prepared_facts.main(ARGV) == 2
    err = capsys.readouterr().err
    assert "prepared facts refused" in err
    assert "Traceback" not in err


class _FakeTransaction:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.in_transaction = True

    def __exit__(self, *_exc: object) -> bool:
        self._conn.in_transaction = False
        return False


class _FakeConnection:
    """Records transaction scope; the collect call must arrive outside it."""

    def __init__(self, home: Path) -> None:
        self._home = home
        self.in_transaction = False
        self.autocommit = False

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    def execute(self, _sql: str, _params: tuple[str, str]) -> Any:
        home = str(self._home)

        class _Cursor:
            def fetchone(self) -> tuple[str]:
                return (home,)

        return _Cursor()

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def test_collection_runs_outside_the_deployment_lock_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F1: the row lock fences lease + registration only; image hashing and the
    receipt write must not hold it -- renewals and the N-unit fan-out would
    otherwise wait behind one unit's filesystem walk."""
    home = (tmp_path / "unit").resolve()
    root = home / "releases" / ARTIFACT
    prefix = root / "venv"
    (home / "run").mkdir(parents=True)
    (home / "machine_name").write_text("runner\n", encoding="utf-8")
    (root / "db").mkdir(parents=True)
    (root / "db/schema.sql").write_text("-- baseline\n", encoding="utf-8")

    expected = ExpectedUnitWriters(
        machine="runner",
        home=str(home),
        artifact_digest=ARTIFACT,
        manifest_digest=MANIFEST,
        processes=(),
        sessions=(),
        launchers=(),
    )
    receipt = PreparationReceipt(
        version=1,
        expected=expected,
        services=(PreparedService(session="ava-ops", requires_db=True, gate=None),),
        excluded_registrations=(),
        inventory_digest=expected.unit().inventory_digest,
        closure="unknown",
        unresolved=("writer closure",),
    )
    body = receipt.model_dump_json().encode("ascii")
    events: list[str] = []
    conn = _FakeConnection(home)

    def connect(_url: str, **_kwargs: object) -> _FakeConnection:
        return conn

    def lock(_conn: object, _operation: object) -> None:
        assert conn.in_transaction, "the lease fence must hold its transaction"
        events.append("lock")

    def collect(
        _conn: object, _image: object, _home: Path, _machine: str, *, schema_digest: str
    ) -> Path:
        assert not conn.in_transaction, "collection must not run inside the lock transaction"
        assert conn.autocommit, "collection reads are lock-free single statements"
        events.append("collect")
        digest = hashlib.sha256(body).hexdigest()
        path = home / "run" / f"release-inventory-{digest}.json"
        path.write_bytes(body)
        return path

    def verify(ref: Any, _store: Path) -> VerifiedRelease:
        release_root = (
            root if ref.artifact_digest == ARTIFACT else home / "releases" / RECOVERY_ARTIFACT
        )
        return VerifiedRelease(
            digest=ref.artifact_digest,
            manifest_digest=ref.manifest_digest,
            root=release_root,
            interpreter=release_root / "venv/bin/python",
            cwd=release_root,
        )

    service = NormalService(
        session="ava-ops",
        module="services.agent_ops.daemon",
        executable=f"{root}/venv/bin/python",
        entrypoint=f"{root}/venv/services/agent_ops/daemon.py",
        command_digest="0" * 64,
    )

    monkeypatch.setattr(prepared_facts.sys, "platform", "linux")
    monkeypatch.setattr(prepared_facts, "WHEEL_RUNTIME", True)
    monkeypatch.setattr(prepared_facts, "runtime_venv", lambda: prefix)
    monkeypatch.setattr(prepared_facts, "__file__", str(prefix / "cli/prepared_facts.py"))

    def schema_digest(_path: Path) -> str:
        return SCHEMA

    monkeypatch.setattr(prepared_facts, "file_sha256", schema_digest)
    monkeypatch.setattr(prepared_facts, "_verify_release", verify)
    monkeypatch.setattr(
        prepared_facts, "psycopg", SimpleNamespace(connect=connect, Error=Exception)
    )
    monkeypatch.setattr(prepared_facts, "lock_rollout", lock)
    monkeypatch.setattr(prepared_facts, "prepare_unit_inventory", collect)

    def normal_services(_unit: object, _schema: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(identity=service)]

    def no_selector(_home: Path) -> None:
        return None

    monkeypatch.setattr(prepared_facts, "prepare_normal_services", normal_services)
    monkeypatch.setattr(prepared_facts, "read_selector", no_selector)
    # The entry reads its database projection live from the environment (the
    # restricted-child contract), so the environment is the real seam here —
    # not the module-load Settings singleton.
    monkeypatch.setitem(os.environ, "AVA_DB_URL", "postgresql://runner@unit/db")

    result = prepared_facts.produce_facts(prepared_facts._parser().parse_args(ARGV))

    unit = cast("dict[str, Any]", result["unit"])
    candidate = cast("dict[str, Any]", result["candidate"])
    services = cast("list[dict[str, Any]]", candidate["services"])
    assert events == ["lock", "collect"]
    assert unit["machine"] == "runner"
    assert services[0]["session"] == "ava-ops"
