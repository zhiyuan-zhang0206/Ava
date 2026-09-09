"""Actual birth boundaries preserve pending work during publication maintenance."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from shared.db import create_agent
from shared.incarnation_resources import ResourceBirth, ResourceEvidenceError
from shared.managed_writer_publication import (
    CurrentAdmission,
    WriterPublication,
)
from shared.resource_birth import require_birth_token
from shared.runtime_admission import (
    PublicationAdmissionDeferredError,
    legacy_boot_terminal_allowed,
    require_activation,
)
from tests.shared.test_managed_writer_publication import publication_db as publication_db
from tests.shared.test_managed_writer_publication import seed_current


@pytest.mark.usefixtures("publication_db")
@pytest.mark.parametrize("missing", ["digest", "challenge", "both", "neither"])
def test_current_requires_both_actual_activation_fields(
    db_conn: psycopg.Connection, missing: str
) -> None:
    current = seed_current(db_conn)
    current = current.model_copy(
        update={
            "activation_digest": None if missing in {"digest", "both"} else "a" * 64,
            "activation_challenge": None if missing in {"challenge", "both"} else uuid4(),
        }
    )
    db_conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s",
        (Jsonb(WriterPublication(current=current).model_dump(mode="json")),),
    )
    decision = CurrentAdmission(current.publication_id)
    with db_conn.transaction():
        if missing == "neither":
            require_activation(db_conn, decision)
        else:
            with pytest.raises(PublicationAdmissionDeferredError, match="verified activation"):
                require_activation(db_conn, decision)


@pytest.mark.usefixtures("publication_db")
def test_generic_terminal_fallback_never_becomes_enabled_after_rollout(
    db_conn: psycopg.Connection,
) -> None:
    with db_conn.transaction():
        assert legacy_boot_terminal_allowed(db_conn)
    seed_current(db_conn)
    with db_conn.transaction():
        assert not legacy_boot_terminal_allowed(db_conn)
    db_conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s::jsonb", ('{"version":99}',)
    )
    with pytest.raises(ValueError), db_conn.transaction():
        legacy_boot_terminal_allowed(db_conn)


@pytest.mark.parametrize("failure", ["missing", "stale", "expired", "force", "none"])
def test_actual_birth_token_keeps_original_deadline_and_exact_attempt(
    db_conn: psycopg.Connection, failure: str
) -> None:
    target = create_agent(db_conn)
    birth = ResourceBirth(
        birth=uuid4(),
        launch_attempts=2,
        launch_deadline=datetime.now(UTC) + timedelta(seconds=-1 if failure == "expired" else 60),
    )
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,incarnation_resources) VALUES(%s,%s,'runner',%s)",
        (
            target,
            "terminated" if failure == "force" else "idling",
            Jsonb(birth.model_dump(mode="json")),
        ),
    )
    token = None if failure == "missing" else (birth.birth, 1 if failure == "stale" else 2)
    if failure == "none":
        require_birth_token(db_conn, target, token)
    else:
        with pytest.raises(ResourceEvidenceError):
            require_birth_token(db_conn, target, token)
    stored = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target,)
    ).fetchone()
    assert stored is not None and stored[0] == birth.model_dump(mode="json")
