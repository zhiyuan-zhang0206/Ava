"""Hop-ledger read: bounded slot parsing, raw-byte digests, and the restricted route gate."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from services.agent_ops import bootstrap
from services.agent_ops.bootstrap import ObserverProjection, PreparedObservation, ledger_response
from shared.hop_ledger import (
    ENVELOPE_VERSION,
    MAX_LEDGER_BYTES,
    LedgerReadError,
    SessionRecordSummary,
    build_ledger_payload,
    envelope_bytes,
    read_boot_id,
    read_recovery_ledger,
    read_session_record,
)
from shared.managed_writer_barrier import RolloutIdentity
from shared.managed_writer_observation import ExpectedUnitWriters, ObservationChallenge
from shared.platform import IS_LINUX
from shared.updater_recovery import BootstrapRecoveryJournal

_TEST_UUID = UUID("11111111-2222-3333-4444-555555555555")


def _journal_payload(stage: str = "candidate_ready") -> dict[str, object]:
    phases = [
        {
            "stage": "prepared",
            "observed_at": "2026-09-20T12:00:00+00:00",
            "monotonic_s": 1.0,
            "pid": 1,
            "elapsed_s": None,
        }
    ]
    if stage != "prepared":
        phases.append(
            {
                "stage": stage,
                "observed_at": "2026-09-20T12:01:00+00:00",
                "monotonic_s": 61.0,
                "pid": 1,
                "elapsed_s": 60.0,
            }
        )
    journal = BootstrapRecoveryJournal.model_validate_json(
        json.dumps(
            {
                "request": "/unit/run/request.json",
                "request_digest": "a" * 64,
                "inventory_digest": "b" * 64,
                "candidate_context_digest": "c" * 64,
                "recovery_context_digest": "d" * 64,
                "stage": stage,
                "cron": "@reboot AVA_HOME=/unit /unit/releases/x/venv/bin/python hop",
                "phases": phases,
            }
        )
    )
    return journal.model_dump(mode="json")


def _write_slot_like_the_writer(
    home: Path,
    *,
    generation: str = "gen-1",
    version: int = 1,
    journal: dict[str, object] | None = None,
) -> Path:
    """Serialize through the writer's own ``json.dump`` call shape."""
    slot = home / "run" / "updater-bootstrap-recovery.json"
    slot.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": version,
        "generation": generation,
        "journal": _journal_payload() if journal is None else journal,
    }
    with slot.open("w") as stream:
        json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
    return slot


def _home(tmp_path: Path) -> Path:
    return tmp_path.resolve()


def _prepared(
    home: Path, *, challenge: UUID | None = None, valid_until: datetime | None = None
) -> PreparedObservation:
    now = datetime.now(UTC)
    return PreparedObservation(
        expected=ExpectedUnitWriters(
            machine="test",
            home=str(home),
            artifact_digest="a" * 64,
            manifest_digest="b" * 64,
            processes=(),
            sessions=(),
            launchers=(),
        ),
        operation=RolloutIdentity(holder="test", acquired_at=now, target_sha="c" * 40),
        challenge=ObservationChallenge(
            challenge=challenge if challenge is not None else uuid4(),
            valid_until=valid_until if valid_until is not None else now + timedelta(minutes=1),
        ),
        schema_digest="d" * 64,
    )


def test_absent_slot_reads_as_positively_absent(tmp_path: Path) -> None:
    assert read_recovery_ledger(_home(tmp_path) / "run" / "updater-bootstrap-recovery.json") is None


def test_written_slot_reads_with_its_raw_bytes_digest(tmp_path: Path) -> None:
    home = _home(tmp_path)
    slot = _write_slot_like_the_writer(home)
    read = read_recovery_ledger(slot)
    assert read is not None
    assert (read.version, read.generation) == (ENVELOPE_VERSION, "gen-1")
    assert read.journal == _journal_payload()
    raw = slot.read_bytes()
    assert read.payload_digest == hashlib.sha256(raw).hexdigest()
    # The collector's recomputation contract: the writer's own serialization
    # round-trips through the served fields.
    assert envelope_bytes(read.version, read.generation, read.journal) == raw


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b"[]",
        b'{"version":1,"generation":"g","journal":{},"extra":1}',
        b'{"version":2,"generation":"g","journal":{}}',
        b'{"version":true,"generation":"g","journal":{}}',
        b'{"version":1,"generation":"","journal":{}}',
        b'{"version":1,"generation":5,"journal":{}}',
        b'{"version":1,"generation":"' + b"g" * 129 + b'","journal":{}}',
        b'{"version":1,"generation":"g","journal":[]}',
        b'{"version":1,"generation":"g","journal":{"stage":"candidate_ready"}}',
    ],
)
def test_malformed_slots_refuse_never_silently_absent(tmp_path: Path, raw: bytes) -> None:
    home = _home(tmp_path)
    slot = home / "run" / "updater-bootstrap-recovery.json"
    slot.parent.mkdir(parents=True)
    slot.write_bytes(raw)
    with pytest.raises(LedgerReadError):
        read_recovery_ledger(slot)


def test_unreadable_shapes_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    home.joinpath("run").mkdir()
    slot = home / "run" / "updater-bootstrap-recovery.json"

    slot.mkdir()  # a directory is never a readable slot
    with pytest.raises(LedgerReadError):
        read_recovery_ledger(slot)
    slot.rmdir()

    slot.write_bytes(b"x")
    foreign = os.getuid() + 1 if hasattr(os, "getuid") else 1
    monkeypatch.setattr(os, "getuid", lambda: foreign)
    with pytest.raises(LedgerReadError):
        read_recovery_ledger(slot)
    monkeypatch.undo()

    slot.unlink()
    real = home / "elsewhere.json"
    real.write_bytes(b"{}")
    slot.symlink_to(real)
    with pytest.raises(LedgerReadError):
        read_recovery_ledger(slot)
    slot.unlink()

    slot.write_bytes(b"x" * (MAX_LEDGER_BYTES + 1))
    with pytest.raises(LedgerReadError):
        read_recovery_ledger(slot)


def test_boot_id_reads_a_uuid_or_reads_unavailable(tmp_path: Path) -> None:
    source = tmp_path / "boot_id"
    value = uuid4()
    source.write_text(f"{value}\n", encoding="ascii")
    assert read_boot_id(source) == value
    source.write_text("not-a-uuid", encoding="ascii")
    assert read_boot_id(source) is None
    assert read_boot_id(tmp_path / "missing") is None


@pytest.mark.skipif(not IS_LINUX, reason="Linux kernel boot identity")
def test_boot_id_on_linux_is_the_kernel_value() -> None:
    assert read_boot_id() == UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())


def test_session_record_summary_states(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "run" / "sessions").mkdir(parents=True)
    record = home / "run" / "sessions" / "ava-ops.json"

    assert read_session_record(home).state == "absent"

    record.write_text(json.dumps({"pid": 321, "create_time": 1.5, "starttime": 99}))
    summary = read_session_record(home)
    assert (summary.state, summary.pid, summary.create_time, summary.starttime) == (
        "ok",
        321,
        1.5,
        99,
    )

    record.write_text(json.dumps({"pid": 321, "create_time": 1.5}))
    summary = read_session_record(home)
    assert (summary.state, summary.starttime) == ("ok", None)

    record.write_text("{broken")
    assert read_session_record(home).state == "invalid"
    record.write_text(json.dumps({"pid": 0, "create_time": 1.5}))
    assert read_session_record(home).state == "invalid"
    record.write_text(json.dumps(["not", "a", "mapping"]))
    assert read_session_record(home).state == "invalid"
    record.unlink()
    record.mkdir()
    assert read_session_record(home).state == "invalid"


def test_non_ok_summaries_carry_no_identity() -> None:
    with pytest.raises(ValueError):
        SessionRecordSummary(state="ok")
    with pytest.raises(ValueError):
        SessionRecordSummary(state="ok", pid=1)
    with pytest.raises(ValueError):
        SessionRecordSummary(state="absent", pid=1)


def test_build_payload_encodes_absent_and_damaged_slots(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "run").mkdir()
    payload = build_ledger_payload(home, _TEST_UUID)
    assert payload["mode"] == "bootstrap_hop_ledger"
    assert payload["challenge"] == str(_TEST_UUID)
    assert (payload["journal_present"], payload["journal_readable"]) == (False, False)
    assert "version" not in payload and "payload_digest" not in payload
    assert payload["session_record"] == {
        "state": "absent",
        "pid": None,
        "create_time": None,
        "starttime": None,
    }

    home.joinpath("run", "updater-bootstrap-recovery.json").write_bytes(b"{nope")
    payload = build_ledger_payload(home, _TEST_UUID)
    assert (payload["journal_present"], payload["journal_readable"]) == (True, False)
    assert "version" not in payload


def test_build_payload_serves_the_exact_written_slot(tmp_path: Path) -> None:
    home = _home(tmp_path)
    slot = _write_slot_like_the_writer(home)
    payload = build_ledger_payload(home, _TEST_UUID)
    assert (payload["journal_present"], payload["journal_readable"]) == (True, True)
    assert payload["version"] == ENVELOPE_VERSION
    assert payload["generation"] == "gen-1"
    assert payload["journal"] == _journal_payload()
    assert payload["payload_digest"] == hashlib.sha256(slot.read_bytes()).hexdigest()
    # JSON-able exactly as the route serializes it.
    assert json.loads(json.dumps(payload))["journal"] == _journal_payload()


@pytest.mark.asyncio
async def test_ledger_route_rejects_invalid_and_unknown_challenges(tmp_path: Path) -> None:
    context = _prepared(_home(tmp_path))
    assert (await ledger_response(context, b"{bad"))[0] == 400
    assert (await ledger_response(context, json.dumps({"challenge": str(uuid4())}).encode()))[
        0
    ] == 409
    expired = _prepared(
        _home(tmp_path),
        challenge=context.challenge.challenge,
        valid_until=datetime.now(UTC) - timedelta(seconds=1),
    )
    body = json.dumps({"challenge": str(expired.challenge.challenge)}).encode()
    assert (await ledger_response(expired, body))[0] == 409


@pytest.mark.asyncio
async def test_ledger_route_serves_challenge_gated_slot(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _write_slot_like_the_writer(home)
    context = _prepared(home)
    status, body, content_type = await ledger_response(
        context, json.dumps({"challenge": str(context.challenge.challenge)}).encode()
    )
    assert status == 200 and content_type == "application/json"
    payload = json.loads(body)
    assert payload["challenge"] == str(context.challenge.challenge)
    assert payload["journal_readable"] is True
    # The digest recomputation the collector performs, end to end.
    assert (
        hashlib.sha256(
            envelope_bytes(payload["version"], payload["generation"], payload["journal"])
        ).hexdigest()
        == payload["payload_digest"]
    )


class _StoppedServer:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def serve_forever(self) -> None:
        return None


def _projection() -> ObserverProjection:
    return ObserverProjection(
        db_url=SecretStr("postgresql://projected.invalid/test"),
        cluster_secret=SecretStr(""),
        ops_port=18106,
    )


def _skip_validate_entry(_context: PreparedObservation, _projection: ObserverProjection) -> None:
    return None


@pytest.mark.asyncio
async def test_serve_mounts_the_ledger_route_with_the_observation_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    _write_slot_like_the_writer(home)
    start = AsyncMock(return_value=_StoppedServer())
    monkeypatch.setattr(bootstrap, "validate_entry", _skip_validate_entry)
    monkeypatch.setattr(bootstrap, "start_daemon_http", start)
    context = _prepared(home)

    await bootstrap.serve(context, _projection())

    awaited = start.await_args
    assert awaited is not None
    routes = awaited.kwargs["extra_routes"]
    assert set(routes) == {
        ("POST", "/ops/bootstrap-observation"),
        ("POST", "/ops/bootstrap-hop-ledger"),
        # The restricted /ops effect delivery (allowlisted kinds only).
        ("POST", "/ops"),
    }
    body = json.dumps({"challenge": str(context.challenge.challenge)}).encode()
    status, served, _content_type = await routes[("POST", "/ops/bootstrap-hop-ledger")](body)
    assert status == 200
    assert json.loads(served)["journal_readable"] is True
