"""Legacy rollout refusals and generic abandoned-maintenance recovery."""

from __future__ import annotations

import httpx
import psycopg
import pytest

# A realistic refusal — the shape `_assert_no_orchestration_in_flight` builds from a
# `DeployWindow.detail`. The assertions below key on the parts an operator acts on.
_REFUSAL = (
    "a deploy is already in flight: machine 'wsl' is running a cluster update "
    "(its orchestration session is alive). Two concurrent deploys defeat the "
    "rollout's own safety. Wait for `ava cluster status` to show every host on the "
    "pin, or re-run with --force if you are certain that deploy is dead."
)


def _clear_update_lock(db_conn: psycopg.Connection) -> None:
    """Reset this module's singleton lock setup without a production escape hatch."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE deployment_state SET holder=NULL, acquired_at=NULL, expires_at=NULL, "
            "settle_hosts=NULL, settle_note=NULL, settle_started_at=NULL, "
            "phase='stable', kind=NULL WHERE id=1"
        )
    db_conn.commit()


# ─── `ava cluster restart` — the same refusal, over HTTP ─────────────────────


def _stub_post(monkeypatch: pytest.MonkeyPatch, resp: httpx.Response) -> None:
    monkeypatch.setattr("shared.http_dial.post", lambda *_a, **_k: resp)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8100")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)


# ─── `ava cluster recover` — the override --force cannot provide ─────────────


def test_recover_clears_a_dead_holders_lock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], db_conn: psycopg.Connection
) -> None:
    """`--force` skips the deploy-window check but not the lock `ava cluster update` takes
    after it, so a crashed orchestration still blocks every deploy until its TTL
    expires — up to 30 minutes on the strength of a dead process."""
    import ops.ops_cluster as _ops
    from cli.commands import _cluster_recover
    from shared.cluster_lock import (
        acquire_update_lock,
        update_lock_holder,
    )

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

        assert _cluster_recover.cmd_cluster_recover() == 0
        assert update_lock_holder() is None  # deployable again
        assert "gateway-host:pid81319" in capsys.readouterr().out  # names what it cleared
    finally:
        _clear_update_lock(db_conn)


def test_recover_refuses_while_the_holder_is_alive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], db_conn: psycopg.Connection
) -> None:
    """The override must not become a way to stomp a rollout that is running fine —
    that would reintroduce the collision the deploy window exists to prevent."""
    import ops.ops_cluster as _ops
    from cli.commands import _cluster_recover
    from shared.cluster_lock import (
        acquire_update_lock,
        update_lock_holder,
    )

    _clear_update_lock(db_conn)
    try:
        acquire_update_lock("gateway-host:pid81319")
        monkeypatch.setattr(
            _ops,
            "_lock_holder_is_live",
            lambda _h, **_kw: True,  # pyright: ignore[reportUnknownArgumentType]
        )  # still running  # pyright: ignore[reportUnknownArgumentType]

        assert _cluster_recover.cmd_cluster_recover() == 1
        assert update_lock_holder() == "gateway-host:pid81319"  # untouched
        assert "live process" in capsys.readouterr().err
    finally:
        _clear_update_lock(db_conn)
