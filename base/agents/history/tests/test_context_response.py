"""Model defaults remain live inputs of the public history response capabilities."""

from dataclasses import replace

import psycopg
from psycopg_pool import ConnectionPool

from base.agents.history.context_breakdown import compute_breakdown
from base.agents.history.context_response import context_breakdown_response, resolve_agent_model
from base.config import ConfigBoot
from base.lm.catalog import ModelCatalog
from base.lm.registry import ModelSpec


def test_context_response_uses_independent_live_defaults(
    model_catalog: ModelCatalog, db_conn: psycopg.Connection
) -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("llm_model", "deepseek-v4-pro")
    second.set_field("llm_model", "deepseek-v4-flash")
    catalog = replace(
        model_catalog,
        models={
            **model_catalog.models,
            "deepseek-v4-pro": ModelSpec(provider="test", context_window=1_000_000),
            "deepseek-v4-flash": ModelSpec(provider="test", spawnable=True, context_window=200_000),
        },
    )
    with ConnectionPool(db_conn.info.dsn, min_size=1, max_size=2) as pool:
        empty = compute_breakdown([], [])
        assert (
            context_breakdown_response(
                pool,
                42,
                empty,
                catalog=catalog,
                default_model_reader=lambda: first.view.lm.llm_model,
            ).max_input_tokens
            == 1_000_000
        )
        assert (
            context_breakdown_response(
                pool,
                42,
                empty,
                catalog=catalog,
                default_model_reader=lambda: second.view.lm.llm_model,
            ).max_input_tokens
            == 200_000
        )
        first.set_field("llm_model", "deepseek-v4-flash")
        assert (
            context_breakdown_response(
                pool,
                42,
                empty,
                catalog=catalog,
                default_model_reader=lambda: first.view.lm.llm_model,
            ).max_input_tokens
            == 200_000
        )
        assert (
            resolve_agent_model(
                pool, 42, catalog=catalog, default_model_reader=lambda: second.view.lm.llm_model
            )
            == "deepseek-v4-flash"
        )


def test_overlay_withdrawal_resolves_without_reading_default(
    model_catalog: ModelCatalog, db_conn: psycopg.Connection
) -> None:
    db_conn.execute("INSERT INTO agents (id,label) VALUES (42,'Agent 42')")
    db_conn.execute(
        "INSERT INTO agents_meta (id,spawner,status,config_overlay) VALUES (42,'user','idling',jsonb_build_object('llm_model','withdrawn-test-model'))"
    )
    db_conn.commit()
    catalog = replace(
        model_catalog,
        models={
            **model_catalog.models,
            "deepseek-v4-flash": ModelSpec(provider="test", spawnable=True, context_window=200_000),
            "withdrawn-test-model": ModelSpec(
                provider="test",
                spawnable=False,
                unavailable_fallback="deepseek-v4-flash",
            ),
        },
    )

    def forbidden() -> str:
        raise AssertionError("An overlay must not read the cluster default")

    with ConnectionPool(db_conn.info.dsn, min_size=1, max_size=2) as pool:
        assert (
            resolve_agent_model(pool, 42, catalog=catalog, default_model_reader=forbidden)
            == "deepseek-v4-flash"
        )
        result = context_breakdown_response(
            pool, 42, compute_breakdown([], []), catalog=catalog, default_model_reader=forbidden
        )
    assert result.max_input_tokens == 200_000
    assert result.total_input_tokens == 0
