"""Actual hosted admission preserves pending work during publication maintenance."""

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from shared.managed_writer_publication import (
    CurrentAdmission,
    WriterPublication,
)
from shared.runtime_admission import (
    PublicationAdmissionDeferredError,
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
