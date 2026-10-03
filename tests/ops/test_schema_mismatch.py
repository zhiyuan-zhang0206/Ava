"""Current schema status must work from an installed image without Git or watchdog history."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import NoReturn, cast

import psycopg
import pytest

from base.api_contracts.status import MachineStatus, SchemaMismatchKind, SchemaMismatchStatus
from base.db import Database
from base.deploy.schema.migration_errors import MigrationLayoutError
from base.deploy.schema.migrations import applied_migration_names
from cli.commands.cluster.control import _schema_mismatch_banner
from ops.cluster_status import ClusterStatus, schema_mismatch


def _unexpected(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("schema diagnosis reached retired pin/Git authority")


@pytest.mark.parametrize(
    ("applied", "required", "kind"),
    [
        ({"base"}, {"base"}, None),
        ({"old"}, {"new"}, "divergent"),
        ({"base", "new"}, {"base"}, "schema-ahead-of-code"),
        ({"base"}, {"base", "new"}, "schema-behind-code"),
    ],
)
def test_current_sets_decide_schema_status(
    applied: set[str], required: set[str], kind: str | None
) -> None:
    mismatch = schema_mismatch.classify(applied, required)
    assert (mismatch.kind if mismatch else None) == kind


def _assert_status_is_visible(status: SchemaMismatchStatus) -> None:
    local = ClusterStatus(
        machine_name="gateway",
        serve_gateway=True,
        serve_agent_runner=False,
        paused=False,
        schema_mismatch=status,
    )
    assert local.model_dump(mode="json")["schema_mismatch"] == status.model_dump()
    machine = MachineStatus(
        name="gateway",
        serve_gateway=True,
        serve_agent_runner=False,
        gateway_url="http://gateway",
        up_since_at=datetime(2026, 9, 26, tzinfo=UTC),
        online=True,
        paused=False,
        schema_mismatch=status,
    )
    banner = _schema_mismatch_banner([machine])
    assert len(banner) == 1
    assert status.kind in banner[0]
    assert status.detail in banner[0]
    assert "blocked round" not in banner[0] and "held back" not in banner[0]


def test_status_and_cli_expose_current_diagnosis_only(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    mismatch = schema_mismatch.classify({"base", "new"}, {"base"})
    assert mismatch is not None
    monkeypatch.setattr(schema_mismatch, "detect", lambda _db: mismatch)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(schema_mismatch, "machine_name", lambda: "gateway")
    status = schema_mismatch.status(database)
    assert status is not None
    assert status.model_dump() == {
        "kind": "schema-ahead-of-code",
        "machine": "gateway",
        "detail": "DB has 1 migration(s) this image lacks",
    }
    _assert_status_is_visible(status)


def test_malformed_real_catalog_is_invalid_in_status_and_cli(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    original = applied_migration_names(db_conn)
    monkeypatch.setattr(schema_mismatch, "required_migration_set", lambda: original)
    assert schema_mismatch.status(database, conn=db_conn) is None

    with db_conn.transaction(force_rollback=True):
        db_conn.execute("ALTER TABLE schema_migrations RENAME COLUMN name TO unexpected_name")
        with pytest.raises(MigrationLayoutError, match="unrecognized shape"):
            applied_migration_names(db_conn)
        status = schema_mismatch.status(database, conn=db_conn)
        assert status is not None
        assert status.kind == "invalid-migration-layout"
        assert "DB migration catalog is invalid" in status.detail
        assert "unrecognized shape" in status.detail
        _assert_status_is_visible(status)

    assert applied_migration_names(db_conn) == original
    assert schema_mismatch.status(database, conn=db_conn) is None


@pytest.mark.parametrize(
    "error", [psycopg.OperationalError("offline"), psycopg.errors.QueryCanceled()]
)
def test_query_failure_is_explicitly_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: psycopg.Error, database: Database
) -> None:
    def failed(_conn: object) -> set[str]:
        raise error

    monkeypatch.setattr(schema_mismatch, "applied_migration_names", failed)
    status = schema_mismatch.status(database, conn=cast(psycopg.Connection, object()))
    assert status is not None
    assert status.kind == "unavailable"
    assert type(error).__name__ in status.detail
    _assert_status_is_visible(status)


def test_invalid_image_layout_is_not_a_set_divergence(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    def applied(_conn: object) -> set[str]:
        return {"base"}

    def invalid() -> set[str]:
        raise MigrationLayoutError("required migration file is absent")

    monkeypatch.setattr(schema_mismatch, "applied_migration_names", applied)
    monkeypatch.setattr(schema_mismatch, "required_migration_set", invalid)
    status = schema_mismatch.status(database, conn=cast(psycopg.Connection, object()))
    assert status is not None
    assert status.kind == "invalid-migration-layout"
    assert "image migration layout is invalid" in status.detail
    _assert_status_is_visible(status)


def test_unexpected_programming_failure_is_not_silently_graded(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
) -> None:
    def failed(_conn: object) -> set[str]:
        raise RuntimeError("programming defect")

    monkeypatch.setattr(schema_mismatch, "applied_migration_names", failed)
    with pytest.raises(RuntimeError, match="programming defect"):
        schema_mismatch.detect(database, conn=cast(psycopg.Connection, object()))


@pytest.mark.parametrize("kind", ["invalid-migration-layout", "unavailable"])
def test_gateway_probe_preserves_degraded_schema_status(
    monkeypatch: pytest.MonkeyPatch, kind: SchemaMismatchKind
) -> None:
    from gateway.cluster import roster_probe
    from gateway.cluster import status as status_route

    diagnosis = SchemaMismatchStatus(kind=kind, machine="gateway", detail="schema evidence failed")
    response = ClusterStatus(
        machine_name="gateway",
        serve_gateway=True,
        serve_agent_runner=False,
        paused=False,
        schema_mismatch=diagnosis,
    )

    async def probe(_name: str, _url: str) -> dict[str, object]:
        return response.model_dump(mode="json")

    def reachable(_name: str) -> None:
        pass

    monkeypatch.setattr(roster_probe, "dispatch_status_probe", probe)
    monkeypatch.setattr(roster_probe, "note_identity_match", reachable)
    row = asyncio.run(
        status_route._probe_agent_runner(
            "gateway", ["gateway"], "http://inert.invalid", datetime.now(UTC), None, None
        )
    )
    assert row.online is True
    assert row.schema_mismatch is not None
    assert row.schema_mismatch == diagnosis
    assert row.model_dump(mode="json")["schema_mismatch"] == diagnosis.model_dump()
    _assert_status_is_visible(row.schema_mismatch)
