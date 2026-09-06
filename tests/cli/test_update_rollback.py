"""Layer 2 — gateway rollback-to-last-known-good on a failed local update.

`_run_gateway_local_update` graceful-stops the gateway before it pulls, so
any pull / uv sync / `ava start` failure leaves the gateway offline. Each
failure must call `_recover_gateway_local` (roll the schema back to the
pre-update snapshot, `git reset --hard` to `from_sha`, uv sync, re-`ava start`)
so the gateway revives and Layer 1's compensating cluster/resume rows deliver.

Three layers tested here, all with monkeypatched seams (no real git / DB DDL /
subprocess):
- `_update_git` wrappers (`current_schema_state`, `rollback_schema_to`) open a
  connection and delegate — assert the delegation, not the DB behavior (that is
  `tests/ava/test_migrations.py`).
- `_recover_gateway_local`: ordering (rollback BEFORE reset — the down files
  vanish after the reset) + the unrecoverable exits (pre-baseline schema, sync /
  start failure) return non-zero without leaving an inconsistent half-state.
- `_run_gateway_local_update`: every failure path invokes recovery and
  returns non-zero; success and a restart-only bounce do not.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from cli import commands as _cli
from cli.commands import _update_git as _git_mod
from cli.commands import _update_recover as _rec
from cli.commands import _update_uv_sync
from cli.commands import update as _up
from shared.migrations import MigrationFailed, RollbackBelowFloor

# Opaque applied-set snapshot passed through the recovery seams (the DB behavior
# is exercised in tests/ava/test_migrations.py; here the stubs ignore its value).
_SNAP = {"00000000T000000_baseline"}


def _prepared_recover(dump: Path | None = None) -> tuple[str, set[str], Path | None]:
    """Recovery evidence supplied by orchestration before the local leg starts."""
    return "FROMSHA", _SNAP, dump


@pytest.fixture(autouse=True)
def _stub_update_lock(monkeypatch: pytest.MonkeyPatch, stub_deploy_lease_identity: None) -> None:
    """The orchestration wrapper takes the cluster update lock; stub it so the
    orchestration-threading tests don't hit / contend the central-DB lock. The
    lock-held test re-stubs `acquire_update_lock` to return False."""
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "release_update_lock", lambda _holder: None)  # pyright: ignore[reportUnknownArgumentType]


class _FakeSubprocess:
    """Stand-in for the `subprocess` module: records each `run` argv and returns a
    configurable returncode per call (keyed by a marker in the argv)."""

    def __init__(self, rc_for) -> None:  # rc_for: Callable[[list[str]], int]
        self.calls: list[list[str]] = []
        self._rc_for = rc_for

    def run(self, args, **_kw):  # type: ignore[no-untyped-def]
        argv = list(args)  # pyright: ignore[reportUnknownArgumentType]
        self.calls.append(argv)  # pyright: ignore[reportUnknownArgumentType]
        return SimpleNamespace(returncode=self._rc_for(argv))


def _is_ava_start(argv: list[str]) -> bool:
    # The start runs through the pty wrapper as a plain argv: [<ava_bin>, "start", ...]
    return len(argv) >= 2 and argv[0].endswith("ava") and argv[1] == "start"


# --- _update_git wrapper delegation ------------------------------------------


def test_current_schema_state_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opens a connection, hands it to shared.migrations.applied_migration_names,
    returns that result (the applied-set snapshot the recovery rolls back to)."""
    import shared.migrations as _mig

    seen: dict[str, object] = {}

    def _applied(conn):  # type: ignore[no-untyped-def]
        seen["conn_truthy"] = conn is not None
        return {"00000000T000000_baseline"}

    monkeypatch.setattr(_mig, "applied_migration_names", _applied)  # pyright: ignore[reportUnknownArgumentType]
    assert _git_mod.current_schema_state() == {"00000000T000000_baseline"}
    assert seen["conn_truthy"] is True


def test_rollback_schema_to_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opens a connection and forwards the keep-set to shared.migrations.rollback_to,
    returning the rolled-back names."""
    import shared.migrations as _mig

    seen: dict[str, object] = {}

    def _rollback_to(conn, keep):  # type: ignore[no-untyped-def]
        seen["keep"] = keep
        seen["conn_truthy"] = conn is not None
        return ["29991231T235959_x"]

    monkeypatch.setattr(_mig, "rollback_to", _rollback_to)  # pyright: ignore[reportUnknownArgumentType]
    keep = {"00000000T000000_baseline"}
    assert _git_mod.rollback_schema_to(keep) == ["29991231T235959_x"]
    assert seen["keep"] == keep
    assert seen["conn_truthy"] is True


def test_rollback_schema_to_local_admin_bypasses_stale_runtime_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Failed credential adoption must not strand schema recovery on the old
    runtime password.  The recovery-only path dials the target database through
    this cluster's passwordless local Postgres admin socket."""
    import psycopg

    from cli.commands import _cluster_instance
    from shared import cluster
    from shared import migrations as _mig
    from shared.config import settings

    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", {"gateway": 16420, "postgres": 16433}),
        gateway_home=str(tmp_path),
        created_at="now",
    )
    seen: dict[str, object] = {}

    class _Connection:
        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def _connect(url: str, **kwargs: object) -> _Connection:
        seen["url"] = url
        seen["kwargs"] = kwargs
        return _Connection()

    def _rollback_to(conn: object, keep: set[str]) -> list[str]:
        seen["conn"] = conn
        seen["keep"] = keep
        return ["29991231T235959_x"]

    def _record(_home: Path) -> cluster.ClusterRecord:
        return record

    def _admin_url(port: int) -> str:
        return f"postgresql://local-admin@/postgres?host=/socket&port={port}"

    monkeypatch.setattr(settings.general, "ava_home", tmp_path)
    monkeypatch.setattr(
        settings.data_plane,
        "db_url",
        "postgresql://ava:stale-password@127.0.0.1:16432/ava_history",
    )
    monkeypatch.setattr(cluster, "get_record", _record)
    monkeypatch.setattr(_cluster_instance, "pg_admin_url", _admin_url)
    monkeypatch.setattr(psycopg, "connect", _connect)
    monkeypatch.setattr(_mig, "rollback_to", _rollback_to)

    keep = {"00000000T000000_baseline"}
    assert _git_mod.rollback_schema_to(keep, local_admin=True) == ["29991231T235959_x"]
    assert seen["url"] == ("postgresql://local-admin@/ava_history?host=/socket&port=16433")
    assert seen["keep"] == keep
    assert seen["conn"] is not None
    assert seen["kwargs"] == {
        "prepare_threshold": None,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "connect_timeout": 5,
    }


# --- _recover_gateway_local --------------------------------------------


def _patch_recover(monkeypatch: pytest.MonkeyPatch, *, rollback, sync_rc=0, start_rc=0):
    """Wire `_recover_gateway_local`'s seams; return (order, fake_subprocess).

    `rollback` is either a list (the rolled-back versions) or an exception to raise.
    `order` records the sequence of side effects so a test can assert the schema
    rollback runs before the git reset.
    """
    order: list[str] = []

    def _rollback_schema_to(target, *, local_admin=False):  # type: ignore[no-untyped-def]
        assert local_admin is True
        order.append("rollback")
        if isinstance(rollback, Exception):
            raise rollback
        return rollback

    def _git_reset_hard(sha):  # type: ignore[no-untyped-def]
        order.append(f"reset:{sha}")

    def _sync(_repo: Path, *, timeout_s: float = 600.0) -> SimpleNamespace:
        order.append("uv-sync")
        return SimpleNamespace(returncode=sync_rc)

    def _import_gate(
        _repo: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ()

    def _rc_for(argv: list[str]) -> int:
        if _is_ava_start(argv):
            order.append("ava-start")
            return start_rc
        return 0

    fake = _FakeSubprocess(_rc_for)
    monkeypatch.setattr(_rec, "rollback_schema_to", _rollback_schema_to)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rec, "git_reset_hard", _git_reset_hard)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rec, "run_uv_sync", _sync)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_update_uv_sync, "editable_import_gate", _import_gate)
    monkeypatch.setattr(_rec, "subprocess", fake)
    return order, fake


def test_recover_happy_path_orders_rollback_before_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered: rollback → reset → uv sync → ava start, in that order, rc 0."""
    order, fake = _patch_recover(monkeypatch, rollback=[23])
    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())
    assert rc == 0
    assert order == ["rollback", "reset:FROMSHA", "uv-sync", "ava-start"]
    # The ordinary restart keeps the existing admission hold until readiness.
    start = next(c for c in fake.calls if _is_ava_start(c))
    assert start[-2:] == ["start", "--persist-services"]


def test_recover_forwards_preserve_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend-only update left the frontend running; recovery must keep skipping
    it (forwarded as --disable-service)."""
    _order, fake = _patch_recover(monkeypatch, rollback=[])
    rc = _rec._recover_gateway_local(
        Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset({"frontend"})
    )
    assert rc == 0
    start = next(c for c in fake.calls if _is_ava_start(c))
    assert start[-2:] == [
        "--disable-service",
        "frontend",
    ]


def test_recover_below_floor_does_not_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-baseline schema (no down to reverse) -> return 1 WITHOUT git reset:
    resetting code to from_sha while the schema is newer would make every daemon
    CodeBehindSchema. Leave code+schema consistent on the new revision for a human."""
    order, fake = _patch_recover(
        monkeypatch, rollback=RollbackBelowFloor("below baseline 22 (target 21)")
    )
    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())
    assert rc == 1
    assert order == ["rollback"]  # no reset, no uv sync, no start
    assert fake.calls == []


def test_recover_message_names_data_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrecoverable schema rollback names the exact pre-update restore point."""
    _patch_recover(monkeypatch, rollback=RollbackBelowFloor("below baseline 22 (target 21)"))
    dump = Path("/x/pre.dump")

    rc = _rec._recover_gateway_local(
        Path("/repo"),
        "FROMSHA",
        _SNAP,
        preserve_sessions=frozenset(),
        data_snapshot=dump,
    )

    assert rc == 1
    message = capsys.readouterr().err
    assert str(dump) in message
    assert "restore: decrypt, then" in message
    assert "pg_restore --clean --if-exists -d <db_url> <decrypted-dump>" in message


def test_recover_migration_failed_mid_rollback_does_not_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A down failing mid-rollback (MigrationFailed) -> return 1 WITHOUT git reset.
    rollback_to aborts the batch atomically, so the schema is unchanged and remains
    consistent with the new revision for fix-forward. Resetting code would create
    CodeBehindSchema; leave the determinate stopped-gateway state for a human (the
    I2 review finding — MigrationError must be caught, not just RollbackBelowFloor,
    or this escapes as a bare traceback)."""
    order, fake = _patch_recover(
        monkeypatch, rollback=MigrationFailed("down migration 0023 (...) failed")
    )
    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())
    assert rc == 1
    assert order == ["rollback"]  # no reset, no uv sync, no start
    assert fake.calls == []


def test_recover_local_admin_connection_failure_does_not_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the recovery-only admin socket itself is unavailable, report the
    gateway as down without resetting old code underneath the newer schema."""
    order, fake = _patch_recover(
        monkeypatch,
        rollback=_git_mod.LocalAdminSchemaConnectionError(
            "local Postgres admin socket unavailable"
        ),
    )

    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())

    assert rc == 1
    assert order == ["rollback"]
    assert fake.calls == []


def test_recover_post_commit_unlock_failure_reports_schema_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An error after the rollback transaction may mean the schema already
    changed. Never call that unchanged or reset code across the ambiguity."""
    order, fake = _patch_recover(
        monkeypatch, rollback=RuntimeError("advisory unlock failed after commit")
    )

    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())

    assert rc == 1
    assert order == ["rollback"]
    assert fake.calls == []
    message = capsys.readouterr().err
    assert "schema state is UNKNOWN" in message
    assert "Verify the applied migration set" in message
    assert "schema is unchanged" not in message


def test_recover_git_reset_failure_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `git reset --hard` that does not land (index-lock timeout / wedged tree /
    mixed tree) is a recovery failure like any other: it used to escape as a bare
    traceback while the gateway sat stopped — the MANUAL INTERVENTION marker the
    rollback/uv-sync/start failures all print never appeared. Return 1, no uv sync,
    no start on a tree that is not last-known-good."""
    order: list[str] = []

    def _rollback_schema_to(target, *, local_admin=False):  # type: ignore[no-untyped-def]
        assert local_admin is True
        order.append("rollback")
        return [23]

    def _git_reset_hard(sha):  # type: ignore[no-untyped-def]
        order.append(f"reset:{sha}")
        raise _git_mod.GitPullFailed(
            "another git process holds .git/index.lock for >30s; refusing to run a "
            "second mutating git op on the same tree"
        )

    monkeypatch.setattr(_rec, "rollback_schema_to", _rollback_schema_to)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_rec, "git_reset_hard", _git_reset_hard)  # pyright: ignore[reportUnknownArgumentType]
    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())
    assert rc == 1
    assert order == ["rollback", "reset:FROMSHA"]  # no uv-sync, no ava-start


def test_recover_uv_sync_failure_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery uv sync fails -> 1, ava start never attempted (gateway DOWN)."""
    order, _fake = _patch_recover(monkeypatch, rollback=[23], sync_rc=1)
    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())
    assert rc == 1
    assert order == ["rollback", "reset:FROMSHA", "uv-sync"]  # no ava-start


def test_recover_start_failure_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery `ava start` on last-known-good itself fails -> 1 (alert + human)."""
    order, _fake = _patch_recover(monkeypatch, rollback=[23], start_rc=1)
    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())
    assert rc == 1
    assert order == ["rollback", "reset:FROMSHA", "uv-sync", "ava-start"]


def test_recover_noop_rollback_still_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start never advanced the schema (rollback returns []) -> still reset + sync +
    restart back to from_sha (e.g. a uv-sync-stage failure)."""
    order, _fake = _patch_recover(monkeypatch, rollback=[])
    rc = _rec._recover_gateway_local(Path("/repo"), "FROMSHA", _SNAP, preserve_sessions=frozenset())
    assert rc == 0
    assert order == ["rollback", "reset:FROMSHA", "uv-sync", "ava-start"]


# --- _run_gateway_local_update recovery wiring -------------------------


def _patch_local_update(
    monkeypatch: pytest.MonkeyPatch,
    *,
    checkout_raises=False,
    sync_rc=0,
    start_rc=0,
    recover_rc=0,
    start_interrupts=False,
):
    """Wire `_run_gateway_local_update`'s seams; return (order, recover_calls).

    `recover_calls` records each `_recover_gateway_local` invocation as
    (from_sha, schema_snapshot, preserve_sessions, data_snapshot). `order` records
    the checkout sequence. `recover_rc` is what the stubbed recovery returns (0 = recovered
    to last-known-good, non-zero = gateway still DOWN).
    """
    order: list[str] = []
    recover_calls: list[tuple[str, set[str], frozenset[str], Path | None]] = []

    monkeypatch.setattr(_up, "_do_stop", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]

    def _checkout(sha):  # type: ignore[no-untyped-def]
        order.append(f"checkout:{sha}")
        if checkout_raises:
            raise _up.GitPullFailed("checkout failed")
        return "FROMSHA"

    monkeypatch.setattr(_up, "git_checkout_sha", _checkout)  # pyright: ignore[reportUnknownArgumentType]

    def _sync(_repo: Path, *, timeout_s: float = 600.0) -> SimpleNamespace:
        return SimpleNamespace(returncode=sync_rc)

    def _import_gate(
        _repo: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(_update_uv_sync, "run_uv_sync", _sync)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_update_uv_sync, "editable_import_gate", _import_gate)

    def _rc_for(argv: list[str]) -> int:
        if _is_ava_start(argv):
            if start_interrupts:
                # What a SIGINT arriving mid-`ava start` looks like from in here: the
                # signal unwinds out of the subprocess call, carrying no returncode.
                raise KeyboardInterrupt
            return start_rc
        return 0

    fake = _FakeSubprocess(_rc_for)
    monkeypatch.setattr(_up, "subprocess", fake)

    # `_run_gateway_local_update` calls `_recover_rc`, which lives in
    # `_update_recover` and calls `_recover_gateway_local` there — so patch the
    # recover fn in `_rec` to exercise the real rc-mapping (0 -> 1 recovered, non-0 ->
    # 2 DOWN) on the way back up.
    def _recover(  # type: ignore[no-untyped-def]
        _repo, from_sha, schema_snapshot, *, preserve_sessions, data_snapshot=None
    ):
        recover_calls.append((from_sha, schema_snapshot, preserve_sessions, data_snapshot))  # pyright: ignore[reportUnknownArgumentType]
        return recover_rc

    monkeypatch.setattr(_rec, "_recover_gateway_local", _recover)  # pyright: ignore[reportUnknownArgumentType]
    return order, recover_calls


def test_local_update_checkout_failure_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """git checkout fails -> recovery invoked with the pre-checkout snapshot; recovered -> rc 1."""
    order, recover_calls = _patch_local_update(monkeypatch, checkout_raises=True)
    rc = _up._run_gateway_local_update(
        Path("/repo"), target_sha="TARGETSHA", pull_recover=_prepared_recover(), pull=True
    )
    assert rc == 1  # recovered to last-known-good (recover returned 0)
    assert recover_calls == [("FROMSHA", _SNAP, frozenset(), None)]
    assert order == ["checkout:TARGETSHA"]


def test_an_interrupt_mid_start_recovers_like_any_other_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt is one of the "ANY failure below" the graceful stop's own comment
    covers, and it is the one that arrives with no returncode to carry the verdict.

    This is the path `ops.controllers.stalled_rollout` drives: a rollout that has
    stopped making progress is SIGINT'd rather than killed, precisely so this recovery
    runs. Left to propagate, the interrupt would skip it and leave the gateway stopped
    on a half-applied transition — checkout moved, migrations not run — which is worse
    than what a failed step produces, and it would make the reclaim's whole claim
    ("a hang becomes a failure") false. So it recovers to last-known-good and reports
    through the same rc the caller already reads.
    """
    _order, recover_calls = _patch_local_update(monkeypatch, start_interrupts=True)

    rc = _up._run_gateway_local_update(
        Path("/repo"), target_sha="TARGETSHA", pull_recover=_prepared_recover(), pull=True
    )

    assert rc == 1  # recovered to last-known-good, exactly as a non-zero start reports
    assert recover_calls == [("FROMSHA", _SNAP, frozenset(), None)]


def test_an_interrupt_with_nothing_to_roll_back_to_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart-only bounce takes no snapshot because it changes no code and no
    schema, so there is no last-known-good to recover to. Swallowing the interrupt
    there would report a recovery that never happened; it propagates to the
    orchestration's `finally`, which still unpauses and resumes."""
    _order, recover_calls = _patch_local_update(monkeypatch, start_interrupts=True)

    with pytest.raises(KeyboardInterrupt):
        _up._run_gateway_local_update(Path("/repo"), pull=False)

    assert recover_calls == []


def test_local_update_stops_after_orchestration_prepared_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local leg consumes prebuilt recovery evidence after its stop begins."""
    order, _recover_calls = _patch_local_update(monkeypatch)
    monkeypatch.setattr(_up, "_do_stop", lambda *_a, **_k: order.append("stop") or 0)  # pyright: ignore[reportUnknownArgumentType]

    rc = _up._run_gateway_local_update(
        Path("/repo"), target_sha="TARGETSHA", pull_recover=_prepared_recover(), pull=True
    )
    assert rc == 0
    assert order == ["stop", "checkout:TARGETSHA"]


def test_local_update_requires_prepared_recovery_before_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pull mode refuses before stop when orchestration did not provide recovery evidence."""
    order, recover_calls = _patch_local_update(monkeypatch)
    monkeypatch.setattr(_up, "_do_stop", lambda *_a, **_k: order.append("stop") or 0)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(ValueError, match="requires pull_recover"):
        _up._run_gateway_local_update(Path("/repo"), target_sha="TARGETSHA", pull=True)

    assert "stop" not in order
    assert recover_calls == []


def test_local_update_uv_sync_failure_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """uv sync fails after a good checkout -> recovery; recovered ok -> rc 1."""
    _order, recover_calls = _patch_local_update(monkeypatch, sync_rc=1)
    rc = _up._run_gateway_local_update(
        Path("/repo"), target_sha="TARGETSHA", pull_recover=_prepared_recover(), pull=True
    )
    assert rc == 1
    assert recover_calls == [("FROMSHA", _SNAP, frozenset(), None)]


def test_local_update_start_failure_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava start` fails (a migration may have applied) -> recovery; recovered ok -> rc 1."""
    dump = Path("/x/pre-update.dump")
    _order, recover_calls = _patch_local_update(monkeypatch, start_rc=1)
    rc = _up._run_gateway_local_update(
        Path("/repo"), target_sha="TARGETSHA", pull_recover=_prepared_recover(dump), pull=True
    )
    assert rc == 1
    assert recover_calls == [("FROMSHA", _SNAP, frozenset(), dump)]


def test_local_update_recovery_failure_returns_down_rc(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava start` fails AND recovery cannot bring the gateway back (recover
    returns non-zero) -> rc 2, the orchestration's 'DOWN, needs a human' signal
    (distinct from rc 1 = recovered, the whole point of propagating the recover rc)."""
    _order, recover_calls = _patch_local_update(monkeypatch, start_rc=1, recover_rc=1)
    rc = _up._run_gateway_local_update(
        Path("/repo"), target_sha="TARGETSHA", pull_recover=_prepared_recover(), pull=True
    )
    assert rc == 2
    assert recover_calls == [("FROMSHA", _SNAP, frozenset(), None)]


def test_local_update_success_does_not_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean update -> rc 0, recovery never called (must not re-start on success)."""
    _order, recover_calls = _patch_local_update(monkeypatch)
    rc = _up._run_gateway_local_update(
        Path("/repo"), target_sha="TARGETSHA", pull_recover=_prepared_recover(), pull=True
    )
    assert rc == 0
    assert recover_calls == []


def test_local_update_requires_target_sha_when_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    """pull=True without a pinned target_sha is a contract violation -> raises."""
    _patch_local_update(monkeypatch)
    with pytest.raises(ValueError, match="requires a target_sha"):
        _up._run_gateway_local_update(Path("/repo"), target_sha=None, pull=True)


def test_backend_only_recovery_skips_frontend(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart_frontend=False -> the frontend session is forwarded to recovery as a
    skip (recovery must not rebuild a UI that was never stopped)."""
    _order, recover_calls = _patch_local_update(monkeypatch, start_rc=1)
    rc = _up._run_gateway_local_update(
        Path("/repo"),
        target_sha="TARGETSHA",
        pull_recover=_prepared_recover(),
        restart_frontend=False,
        pull=True,
    )
    assert rc == 1
    assert recover_calls == [("FROMSHA", _SNAP, frozenset({"frontend"}), None)]


def test_restart_only_start_failure_does_not_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart-only bounce (pull=False) changed no code/schema -> nothing to roll
    back; a failed start returns its raw rc with no recovery attempt."""
    _order, recover_calls = _patch_local_update(monkeypatch, start_rc=1)
    rc = _up._run_gateway_local_update(Path("/repo"), restart_frontend=True, pull=False)
    assert rc == 1
    assert recover_calls == []


# --- SHA-pin threading (orchestration -> local update + Phase B payload) ------


def test_orchestration_resolves_and_threads_target_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """The orchestration resolves ONE target_sha (origin/main) and threads the same
    sha to the gateway local update AND every agent-runner's Phase-B self-update
    — the core of the SHA-pin: no node re-resolves a tip that could move mid-rollout."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(_up, "git_resolve_origin_main", lambda: "PINNEDSHA")
    # the orchestration vets the target's migrations/ layout (git read) before Phase A;
    # the synthetic PINNEDSHA is not a real object, so pass the vet here.
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None)])
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_fan_out", lambda *_a, **_k: [("a", "ok", "")])  # pyright: ignore[reportUnknownArgumentType]

    def _local(  # type: ignore[no-untyped-def]
        _repo,
        *,
        target_sha,
        pull_recover,
        restart_frontend,
        pull=True,
        force_reap_agents=False,
        origin="",
    ):
        captured["local_target"] = target_sha
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_local_update", _local)  # pyright: ignore[reportUnknownArgumentType]

    def _phase_b(_hosts, *, target_sha, restart_only, force_reap=False, host_outcomes=None):  # type: ignore[no-untyped-def]
        captured["phaseb_target"] = target_sha
        return {"a": _cli.PollVerdict("ok")}

    monkeypatch.setattr(_up, "_phase_b_and_poll", _phase_b)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert captured["local_target"] == "PINNEDSHA"
    assert captured["phaseb_target"] == "PINNEDSHA"


def test_orchestration_resolve_failure_aborts_before_pausing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If origin/main can't be resolved, the rollout aborts BEFORE Phase A — nothing
    paused, no local update."""
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])

    def _boom() -> str:
        raise _up.GitPullFailed("network down")

    monkeypatch.setattr(_up, "git_resolve_origin_main", _boom)
    monkeypatch.setattr(
        _cli, "_list_agent_runners", lambda: pytest.fail("must abort before Phase A")
    )
    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 1


def test_phase_b_payload_carries_target_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """_phase_b_and_poll forwards target_sha in each agent-runner's cluster_update payload
    (so the host force-checks-out the pinned commit, not its own origin/main)."""
    captured: dict[str, object] = {}

    def _fan_out(hosts, path, timeout, payload=None):  # type: ignore[no-untyped-def]
        captured["payload"] = payload
        captured["timeout"] = timeout
        return [(h[0], "ok", "") for h in hosts]

    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_poll_until_unpaused",
        lambda _hosts, **_unused: {"a": _cli.PollVerdict("ok")},  # pyright: ignore[reportUnknownArgumentType]
    )
    _up._phase_b_and_poll([("a", None)], target_sha="PINNEDSHA", restart_only=False)
    assert captured["payload"] == {"target_sha": "PINNEDSHA"}
    assert captured["timeout"] == 120.0


def test_orchestration_aborts_when_update_lock_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second gateway update that finds the cluster update lock held by a live
    holder aborts before doing anything — #2 (serialize updates; the 2026-06-01
    collision was two gateway updates racing). Overrides the autouse stub."""
    monkeypatch.setattr(_up, "acquire_update_lock", lambda _holder, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "update_lock_holder", lambda: "cloud:pid999")
    monkeypatch.setattr(
        _cli, "_changed_paths_vs_origin", lambda: pytest.fail("must abort before classify")
    )
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: pytest.fail("must not pause anything"))
    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 1
