"""The watchdog's loop structure: the TaskGroup that owns the four loops, the
sequential round runner, the bounded fan-out, and the attempt clocks."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.config.service_read import ConfigAuthority
from base.daemon.loop_health import LoopProgress
from base.lm.catalog import ModelCatalog
from ops.cluster.rpc import worst_case_dispatch_seconds
from services.wake.delivery_watchdog import attempts, rounds


@pytest.fixture
def pool():
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=4, open=True)
    try:
        yield p
    finally:
        p.close()


def _progress() -> LoopProgress:
    return LoopProgress("test", rounds.loop_liveness_timeout_s())


def _agent(
    db: psycopg.Connection, *, model_catalog: ModelCatalog, config_authority: ConfigAuthority
) -> int:
    from tests.fixtures.units import spawn_agent

    return spawn_agent(spawner="user", catalog=model_catalog, authority=config_authority)


def test_claim_hands_an_agent_out_once_per_cooldown(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    aid = _agent(db_conn, model_catalog=model_catalog, config_authority=config_authority)

    assert attempts.claim_attempts(pool, attempts.HARVEST, [aid], 60.0) == ([aid], 0)
    assert attempts.claim_attempts(pool, attempts.HARVEST, [aid], 60.0) == ([], 0)
    # Each loop kind keeps its own clock.
    assert attempts.claim_attempts(pool, attempts.RESURRECT, [aid], 60.0) == ([aid], 0)

    db_conn.execute(
        "UPDATE delivery_watchdog_attempts SET last_attempt_at = now() - interval '61 seconds' "
        "WHERE kind = 'harvest' AND agent_id = %s",
        (aid,),
    )
    db_conn.commit()
    assert attempts.claim_attempts(pool, attempts.HARVEST, [aid], 60.0) == ([aid], 0)


def test_claim_limit_defers_the_ready_agents_it_leaves(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    first, second, third = (
        _agent(db_conn, model_catalog=model_catalog, config_authority=config_authority),
        _agent(db_conn, model_catalog=model_catalog, config_authority=config_authority),
        _agent(db_conn, model_catalog=model_catalog, config_authority=config_authority),
    )
    assert attempts.claim_attempts(pool, attempts.RESURRECT, [first], 60.0) == ([first], 0)

    claimed, deferred = attempts.claim_attempts(
        pool, attempts.RESURRECT, [first, second, third], 60.0, limit=1
    )

    # `first` is cooling and is neither claimed nor counted as deferred.
    assert (claimed, deferred) == ([second], 1)


def test_claim_never_hands_out_an_unknown_agent(pool: ConnectionPool) -> None:
    assert attempts.claim_attempts(pool, attempts.HARVEST, [2_000_000_000], 60.0) == ([], 0)


def test_finish_restarts_the_cooldown_clock(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    aid = _agent(db_conn, model_catalog=model_catalog, config_authority=config_authority)
    attempts.claim_attempts(pool, attempts.HOSTED_TURN, [aid], 600.0)
    db_conn.execute(
        "UPDATE delivery_watchdog_attempts SET last_attempt_at = now() - interval '1 hour' "
        "WHERE kind = 'hosted_turn' AND agent_id = %s",
        (aid,),
    )
    db_conn.commit()

    attempts.finish_attempt(pool, attempts.HOSTED_TURN, aid)

    assert attempts.claim_attempts(pool, attempts.HOSTED_TURN, [aid], 600.0) == ([], 0)


def test_the_job_deadline_is_derived_from_the_cluster_rpc_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.gateway, "cluster_rpc_timeout_seconds", 30.0)
    monkeypatch.setattr(settings.gateway, "cluster_rpc_max_retries", 3)

    # Four attempts of 30s, plus the 0.5 + 1 + 2 second backoffs at their +50% jitter ceiling.
    assert worst_case_dispatch_seconds() == pytest.approx(4 * 30.0 + 3.5 * 1.5)
    assert rounds.rpc_deadline_s() == pytest.approx(2 * worst_case_dispatch_seconds())
    assert rounds.loop_liveness_timeout_s() > rounds.rpc_deadline_s()

    monkeypatch.setattr(settings.gateway, "cluster_rpc_max_retries", 0)
    assert worst_case_dispatch_seconds() == pytest.approx(30.0)
