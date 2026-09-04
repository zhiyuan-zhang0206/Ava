# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import psycopg
import pytest
from pydantic import SecretStr, ValidationError

from cli.commands import _release_inventory as inventory
from cli.commands import _update_bootstrap as bootstrap
from shared.managed_writer_observation import (
    ExpectedLauncher,
    ExpectedProcess,
    ExpectedSession,
    ExpectedUnitWriters,
)
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease


def _expected(process: ExpectedProcess) -> ExpectedUnitWriters:
    session = ExpectedSession(name="ava-ops", process=process)
    return ExpectedUnitWriters(
        machine="machine",
        home="/unit",
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        processes=(process,),
        sessions=(session,),
        launchers=(ExpectedLauncher(kind="crontab", name="job", definition_digest="c" * 64),),
    )


def _inventory(expected: ExpectedUnitWriters) -> dict[str, object]:
    return {
        "version": 1,
        "expected": expected.model_dump(mode="json"),
        "services": [{"session": "ava-ops", "requires_db": True, "gate": None}],
        "inventory_digest": expected.unit().inventory_digest,
        "closure": "unknown",
        "unresolved": ["retained"],
    }


def test_resume_inventory_allows_only_the_verified_observer_substitution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = _expected(ExpectedProcess(pid=11, create_time=1.0, starttime=1))
    live = ExpectedSession(
        name="ava-ops", process=ExpectedProcess(pid=22, create_time=2.0, starttime=2)
    )
    prepared = _inventory(before)
    encoded = json.dumps(prepared, sort_keys=True, separators=(",", ":")).encode()
    receipt = tmp_path / "run" / f"release-inventory-{hashlib.sha256(encoded).hexdigest()}.json"
    receipt.parent.mkdir()
    receipt.write_bytes(encoded)
    current = _inventory(
        before.model_copy(update={"processes": (live.process,), "sessions": (live,)})
    )
    monkeypatch.setattr(inventory, "collect_inventory", lambda *_args, **_kwargs: current)

    result = inventory.revalidate_bootstrap_inventory(
        cast("psycopg.Connection", None),
        cast("VerifiedRelease", object()),
        tmp_path,
        "machine",
        receipt,
        current_session=live,
        schema_digest="d" * 64,
    )
    assert result == before

    current["services"] = []
    with pytest.raises(ReleaseRejectedError, match="static facts"):
        inventory.revalidate_bootstrap_inventory(
            cast("psycopg.Connection", None),
            cast("VerifiedRelease", object()),
            tmp_path,
            "machine",
            receipt,
            current_session=live,
            schema_digest="d" * 64,
        )


def test_phase_evidence_is_bounded() -> None:
    phase = {
        "stage": "prepared",
        "observed_at": datetime.now(UTC).isoformat(),
        "monotonic_s": 1.0,
        "pid": 1,
        "elapsed_s": None,
    }
    with pytest.raises(ValidationError):
        bootstrap.BootstrapJournal.model_validate(
            {
                "request": "/unit/run/request",
                "request_digest": "a" * 64,
                "inventory_digest": "b" * 64,
                "candidate_context_digest": "c" * 64,
                "recovery_context_digest": "d" * 64,
                "stage": "prepared",
                "cron": "",
                "phases": [phase] * 65,
            }
        )


def test_child_projection_preserves_transport_encryption() -> None:
    plan = SimpleNamespace(
        candidate=SimpleNamespace(expected=SimpleNamespace(home="/unit")),
        projection=SimpleNamespace(
            db_url=SecretStr("postgresql://runner"),
            cluster_secret=SecretStr("secret"),
            ops_port=9000,
            transport_encryption="overlay",
        ),
    )
    assert (
        bootstrap._child_environment(cast("bootstrap.PreparedBootstrapHop", plan))[
            "AVA_TRANSPORT_ENCRYPTION"
        ]
        == "overlay"
    )


def test_fork_before_record_is_ambiguous() -> None:
    old = ExpectedProcess(pid=11, create_time=1.0, starttime=1)
    assert bootstrap._candidate_start_is_ambiguous("candidate_starting", None, old)
    assert bootstrap._candidate_start_is_ambiguous("candidate_starting", (old, "A"), old)


def test_launch_exception_before_record_never_starts_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = ExpectedProcess(pid=11, create_time=1.0, starttime=1)
    plan = SimpleNamespace(
        candidate=SimpleNamespace(
            challenge=SimpleNamespace(valid_until=datetime.now(UTC) + timedelta(minutes=1))
        ),
        projection=object(),
        image=object(),
        request=SimpleNamespace(candidate_context="/unit/run/candidate"),
        old_session=SimpleNamespace(process=old),
        journal=None,
        validation_seconds=0.0,
    )
    stage = ""
    starts = 0

    def record_stage(_plan: object, _generation: str, value: str, _cron: bytes) -> None:
        nonlocal stage
        stage = value

    def launch(*_args: object, **_kwargs: object) -> None:
        nonlocal starts
        starts += 1
        raise RuntimeError("fork returned before record publication")

    def recovery() -> dict[str, object]:
        journal = {
            "request": "/unit/run/request",
            "request_digest": "a" * 64,
            "inventory_digest": "b" * 64,
            "candidate_context_digest": "c" * 64,
            "recovery_context_digest": "d" * 64,
            "stage": stage,
            "cron": "",
            "phases": [],
        }
        return {"version": 1, "generation": "g", "journal": journal}

    monkeypatch.setattr(bootstrap, "validate_operation", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_cron_tables", lambda _plan: (b"", b""))
    monkeypatch.setattr(bootstrap, "_journal", record_stage)
    monkeypatch.setattr(bootstrap, "_replace_cron", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_stop_old_observer", lambda _plan: None)
    monkeypatch.setattr(bootstrap, "_start_observer", launch)
    monkeypatch.setattr(
        bootstrap,
        "_recorded_observer",
        lambda _plan: (_ for _ in ()).throw(ReleaseRejectedError("no new exact record")),
    )
    monkeypatch.setattr(bootstrap, "_restore_a", lambda *_args: pytest.fail("second launch"))
    monkeypatch.setattr(bootstrap.updater_handoff, "read_bootstrap_recovery", recovery)

    with pytest.raises(ReleaseRejectedError, match="spawn is ambiguous"):
        bootstrap._execute_bootstrap_hop(cast("bootstrap.PreparedBootstrapHop", plan), "g")
    assert starts == 1
