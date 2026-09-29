"""`ava cluster recover` clears only an abandoned maintenance lock."""

from __future__ import annotations

import psycopg
import pytest


def _clear_update_lock(db_conn: psycopg.Connection) -> None:
    """Reset this module's singleton lock setup without a production escape hatch."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE deployment_state SET holder=NULL, acquired_at=NULL, expires_at=NULL, "
            "settle_hosts=NULL, settle_note=NULL, settle_started_at=NULL, "
            "phase='stable', kind=NULL WHERE id=1"
        )
    db_conn.commit()


# ─── `ava cluster recover` — the override --force cannot provide ─────────────


def test_recover_clears_a_dead_holders_lock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], db_conn: psycopg.Connection
) -> None:
    """`--force` skips the deploy-window check but not the lock `ava cluster update` takes
    after it, so a crashed orchestration still blocks every deploy until its TTL
    expires — up to 30 minutes on the strength of a dead process."""
    import ops.cluster as _ops
    from base.deploy.state.cluster_lock import (
        acquire_update_lock,
        update_lock_holder,
    )
    from cli.commands.cluster import recover

    _clear_update_lock(db_conn)
    try:
        acquire_update_lock("gateway-host:pid81319")
        monkeypatch.setattr(
            _ops,
            "_lock_holder_is_live",
            lambda _h, **_kw: False,  # pyright: ignore[reportUnknownArgumentType]
        )  # the holder died  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_ops, "updater_lease_live", lambda: False)
        monkeypatch.setattr(_ops, "unpause_local_cluster", lambda: None)

        assert recover.cmd_cluster_recover() == 0
        assert update_lock_holder() is None  # deployable again
        out = capsys.readouterr().out
        assert "gateway-host:pid81319" in out  # names what it cleared
        assert "ava cluster update" not in out  # never a bare verb that exits 2
    finally:
        _clear_update_lock(db_conn)


def test_recover_refuses_while_the_holder_is_alive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], db_conn: psycopg.Connection
) -> None:
    """The override must not become a way to stomp a rollout that is running fine —
    that would reintroduce the collision the deploy window exists to prevent."""
    import ops.cluster as _ops
    from base.deploy.state.cluster_lock import (
        acquire_update_lock,
        update_lock_holder,
    )
    from cli.commands.cluster import recover

    _clear_update_lock(db_conn)
    try:
        acquire_update_lock("gateway-host:pid81319")
        monkeypatch.setattr(
            _ops,
            "_lock_holder_is_live",
            lambda _h, **_kw: True,  # pyright: ignore[reportUnknownArgumentType]
        )  # still running  # pyright: ignore[reportUnknownArgumentType]

        assert recover.cmd_cluster_recover() == 1
        assert update_lock_holder() == "gateway-host:pid81319"  # untouched
        assert "live process" in capsys.readouterr().err
    finally:
        _clear_update_lock(db_conn)
