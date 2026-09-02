"""A refused deploy must read as a refusal, not as a crash.

`ops/deploy_window.py` gives the deploy-window refusal a carefully built message —
which host is in the way, why two concurrent deploys defeat the rollout's own
safety, and the `--force` override. Neither CLI trigger handled it, so all of that
reached the operator underneath a Python traceback:

- `ava cluster update` on a gateway calls `spawn_rollout`, which raises
  `ClusterUpdateInProgress` (or `NothingToUpdate`); `cli/main.py` catches only
  `ValidationError`, so both unwound to the terminal as a stack trace.
- `ava cluster restart` POSTs the gateway, which returns the same text as a 409
  `detail` — and `raise_for_status()` discarded the body, so the operator got
  `Client error '409 Conflict' for url ...` and never saw which host was deploying.

A stack trace reads as "the tool broke", not "the tool refused", which is the exact
opposite of what a second operator needs at that moment. These tests pin both seams:
the message is surfaced, and the exit code is non-zero.
"""

from __future__ import annotations

import httpx
import pytest

from cli.commands import cluster as _cluster

# A realistic refusal — the shape `_assert_no_orchestration_in_flight` builds from a
# `DeployWindow.detail`. The assertions below key on the parts an operator acts on.
_REFUSAL = (
    "a deploy is already in flight: machine 'wsl' is running a cluster update "
    "(its orchestration session is alive). Two concurrent deploys defeat the "
    "rollout's own safety. Wait for `ava cluster status` to show every host on the "
    "pin, or re-run with --force if you are certain that deploy is dead."
)


def test_update_reports_a_refused_deploy_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The thin-client trigger (issue #216). Exit 1, and the whole refusal —
    including which host is deploying and the --force escape — lands in the
    operator's own terminal from the gateway's 409 body."""

    class _Refused:
        status_code = 409

        def raise_for_status(self) -> None:
            req = httpx.Request("POST", "http://gw:8000/api/cluster/rollout")
            raise httpx.HTTPStatusError(
                "conflict", request=req, response=httpx.Response(409, request=req)
            )

        def json(self) -> dict[str, str]:
            return {"detail": _REFUSAL}

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", lambda *_a, **_k: _Refused())  # pyright: ignore[reportUnknownArgumentType]

    from cli.commands import cmd_update

    rc = cmd_update()

    assert rc == 1
    out = capsys.readouterr()
    assert "wsl" in out.err  # which host is in the way
    assert "--force" in out.err  # and the way past it
    assert "dispatched" not in out.out  # never claims success


def test_update_force_is_threaded_through_the_new_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The POST body must carry `--force` — the override is the whole reason
    a legitimately-stuck operator can proceed."""
    from typing import cast

    seen: list[bool] = []

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-rollout", "log": "/var/log/rollout.log"}

    def _post(url: str, **kw: object) -> _Resp:
        body = cast(dict[str, object], kw["json"])
        seen.append(cast(bool, body["force"]))
        return _Resp()

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", _post)  # pyright: ignore[reportUnknownArgumentType]

    from cli.commands import cmd_update

    assert cmd_update(force=True) == 0
    assert seen == [True]


def test_nothing_to_update_is_a_no_op_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An already-up-to-date cluster is the other ordinary answer to `ava cluster update`.
    Exit 0, so a scripted update in a chain is not tripped by a no-op."""

    class _Nothing:
        status_code = 422

        def raise_for_status(self) -> None:
            req = httpx.Request("POST", "http://gw:8000/api/cluster/rollout")
            raise httpx.HTTPStatusError(
                "nothing", request=req, response=httpx.Response(422, request=req)
            )

        def json(self) -> dict[str, str]:
            return {"detail": "cluster is already up to date — nothing to roll out"}

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", lambda *_a, **_k: _Nothing())  # pyright: ignore[reportUnknownArgumentType]

    from cli.commands import cmd_update

    assert cmd_update() == 0
    assert "already up to date" in capsys.readouterr().err


def test_a_dispatched_rollout_still_reports_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path is untouched by the wrapping."""

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-rollout", "log": "/var/log/rollout.log"}

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", lambda *_a, **_k: _Resp())  # pyright: ignore[reportUnknownArgumentType]

    from cli.commands import cmd_update

    assert cmd_update() == 0


def test_dispatched_replay_names_the_half_deployed_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bookmark disagreement is a repair rollout, never an ordinary no-op."""

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {
                "session": "ava-rollout",
                "log": "/var/log/rollout.log",
                "needs_replay": True,
            }

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", lambda *_a, **_k: _Resp())  # pyright: ignore[reportUnknownArgumentType]

    from cli.commands import cmd_update

    assert cmd_update() == 0
    assert "half-deployed state" in capsys.readouterr().out


# ─── `ava cluster restart` — the same refusal, over HTTP ─────────────────────


def _stub_post(monkeypatch: pytest.MonkeyPatch, resp: httpx.Response) -> None:
    monkeypatch.setattr("shared.http_dial.post", lambda *_a, **_k: resp)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8100")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)


def test_cluster_restart_surfaces_the_409_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`raise_for_status()` discarded the body — the one place the gateway put the
    refusal. The operator saw a bare 409 and never learned which host was deploying."""
    req = httpx.Request("POST", "http://gw:8100/api/cluster/restart")
    _stub_post(monkeypatch, httpx.Response(409, request=req, json={"detail": _REFUSAL}))

    rc = _cluster.cmd_cluster_restart()

    assert rc == 1
    err = capsys.readouterr().err
    assert "wsl" in err
    assert "--force" in err


def test_cluster_restart_still_fails_fast_on_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the 409 is a refusal. A 503 (backend unavailable) or any other status is a
    real failure and must still raise rather than be quietly swallowed."""
    req = httpx.Request("POST", "http://gw:8100/api/cluster/restart")
    _stub_post(monkeypatch, httpx.Response(503, request=req, json={"detail": "backend gone"}))

    with pytest.raises(httpx.HTTPStatusError):
        _cluster.cmd_cluster_restart()


def test_cluster_restart_happy_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    req = httpx.Request("POST", "http://gw:8100/api/cluster/restart")
    _stub_post(
        monkeypatch,
        httpx.Response(200, request=req, json={"session": "ava-cluster-restart", "log": "/l"}),
    )
    assert _cluster.cmd_cluster_restart() == 0


def test_conflict_detail_survives_a_body_it_did_not_expect() -> None:
    """A proxy's HTML error page must still yield something actionable rather than an
    empty line or a JSON decode traceback — this helper runs only on the path whose
    job is explaining a refusal."""
    req = httpx.Request("POST", "http://gw:8100/api/cluster/restart")
    assert "409" in _cluster._conflict_detail(httpx.Response(409, request=req, text=""))
    assert "gateway" in _cluster._conflict_detail(
        httpx.Response(409, request=req, json={"unexpected": "shape"})
    )
    assert "<html>" in _cluster._conflict_detail(
        httpx.Response(409, request=req, text="<html>gateway timeout</html>")
    )


# ─── `ava cluster recover` — the override --force cannot provide ─────────────


def test_recover_clears_a_dead_holders_lock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--force` skips the deploy-window check but not the lock `ava cluster update` takes
    after it, so a crashed orchestration still blocks every deploy until its TTL
    expires — up to 30 minutes on the strength of a dead process."""
    import ops.ops_cluster as _ops
    from cli.commands import _cluster_recover
    from shared.cluster_lock import (
        acquire_update_lock,
        force_release_update_lock,
        update_lock_holder,
    )

    force_release_update_lock()
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
        force_release_update_lock()


def test_recover_refuses_while_the_holder_is_alive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The override must not become a way to stomp a rollout that is running fine —
    that would reintroduce the collision the deploy window exists to prevent."""
    import ops.ops_cluster as _ops
    from cli.commands import _cluster_recover
    from shared.cluster_lock import (
        acquire_update_lock,
        force_release_update_lock,
        update_lock_holder,
    )

    force_release_update_lock()
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
        force_release_update_lock()
