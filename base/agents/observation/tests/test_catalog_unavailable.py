from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from base.agents.observation.roster import AgentCard, list_directory, select_roster
from base.agents.observation.snapshot import AgentSnapshot, select_one
from base.lm.catalog import ModelCatalog


def forbidden_default() -> str:
    raise AssertionError("Unavailable capability metadata must not resolve a default model")


@pytest.fixture
def existing_agents(db_conn: psycopg.Connection) -> psycopg.Connection:
    db_conn.execute("INSERT INTO agents (id) VALUES (1), (2), (3)")
    db_conn.execute(
        "INSERT INTO agents_meta (id,spawner,status) VALUES "
        "(1,'user','terminated'), (2,'agent:1','running'), (3,'user','idling')"
    )
    db_conn.commit()
    return db_conn


def test_unavailable_capabilities_preserve_ancestors_and_keyset_history(
    existing_agents: psycopg.Connection,
) -> None:
    roster = select_roster(existing_agents, catalog=None, default_model_reader=forbidden_default)
    assert [card.agent_id for card in roster.agents] == [2, 3]
    assert all(card.supports_vision is None for card in roster.agents)
    assert [link.agent_id for link in roster.ancestors] == [1]
    first = list_directory(
        existing_agents, scope="all", limit=2, catalog=None, default_model_reader=forbidden_default
    )
    second = list_directory(
        existing_agents,
        scope="all",
        limit=2,
        before_id=first.next_cursor,
        catalog=None,
        default_model_reader=forbidden_default,
    )
    assert [card.agent_id for card in first.agents] == [3, 2]
    assert [card.agent_id for card in second.agents] == [1]
    assert second.next_cursor is None
    detail = select_one(existing_agents, 1, catalog=None, default_model_reader=forbidden_default)
    assert detail is not None and detail.supports_vision is None and detail.status == "terminated"
    assert (
        select_one(existing_agents, 99, catalog=None, default_model_reader=forbidden_default)
        is None
    )


@pytest.mark.parametrize("reader", ["roster", "directory", "detail"])
@pytest.mark.parametrize("column", ["birth_config", "config_overlay"])
@pytest.mark.parametrize("pins", [[], {"llm_model": 7}, {"llm_model": None}])
def test_catalog_failure_does_not_hide_damaged_model_pins(
    existing_agents: psycopg.Connection,
    reader: str,
    column: str,
    pins: Any,
) -> None:
    existing_agents.execute(
        sql.SQL("UPDATE agents_meta SET {}=%s WHERE id=2").format(sql.Identifier(column)),
        (Jsonb(pins),),
    )
    with pytest.raises(ValueError, match=column):
        if reader == "roster":
            select_roster(existing_agents, catalog=None, default_model_reader=forbidden_default)
        elif reader == "directory":
            list_directory(existing_agents, catalog=None, default_model_reader=forbidden_default)
        else:
            select_one(existing_agents, 2, catalog=None, default_model_reader=forbidden_default)


def test_nullable_capability_is_required_and_does_not_relax_agent_enums(
    existing_agents: psycopg.Connection,
) -> None:
    card = select_roster(
        existing_agents, catalog=None, default_model_reader=forbidden_default
    ).agents[0]
    detail = select_one(existing_agents, 2, catalog=None, default_model_reader=forbidden_default)
    assert detail is not None
    for model, data in ((AgentCard, card.model_dump()), (AgentSnapshot, detail.model_dump())):
        with pytest.raises(ValidationError, match="status"):
            model.model_validate(data | {"status": "invented-state"})
        with pytest.raises(ValidationError, match="liveness_state"):
            model.model_validate(data | {"liveness_state": "invented-liveness"})
        del data["supports_vision"]
        with pytest.raises(ValidationError, match="supports_vision"):
            model.model_validate(data)


def test_read_errors_are_not_replaced_by_empty_agents(existing_agents: psycopg.Connection) -> None:
    closed = psycopg.connect(existing_agents.info.dsn)
    closed.close()
    for read in (select_roster, list_directory):
        with pytest.raises(psycopg.OperationalError):
            read(closed, catalog=None, default_model_reader=forbidden_default)
    with pytest.raises(psycopg.OperationalError):
        select_one(closed, 2, catalog=None, default_model_reader=forbidden_default)


@pytest.mark.parametrize(
    "model, expected", [("unknown-model", False), ("claude-future-fixture", True)]
)
def test_healthy_catalog_preserves_unknown_model_capability_policy(
    existing_agents: psycopg.Connection,
    model_catalog: ModelCatalog,
    model: str,
    expected: bool,
) -> None:
    existing_agents.execute(
        "UPDATE agents_meta SET birth_config=%s",
        (Jsonb({"llm_model": model}),),
    )
    for read in (select_roster, list_directory):
        assert all(
            card.supports_vision is expected
            for card in read(
                existing_agents, catalog=model_catalog, default_model_reader=forbidden_default
            ).agents
        )
    detail = select_one(
        existing_agents, 2, catalog=model_catalog, default_model_reader=forbidden_default
    )
    assert detail is not None and detail.supports_vision is expected
