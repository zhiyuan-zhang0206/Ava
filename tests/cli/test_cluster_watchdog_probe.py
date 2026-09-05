"""cli.commands._cluster_watchdog_probe — the revive decision itself.

Pins the contract the OS scheduler depends on: a live watchdog is left strictly
alone (a probe that respawned a healthy watchdog every minute would be worse
than no probe), a dead one is respawned from the ops roster's own spec, and a
failed respawn is reported rather than swallowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import _cluster_watchdog_probe as wp
from ops.service_spec import ServiceSpec


def _spec_for(role: str, pidfile: Path | None) -> ServiceSpec:
    """A real ServiceSpec, so a field rename in ops.spec breaks these tests
    rather than letting a stand-in keep them passing against a stale shape."""
    return ServiceSpec(
        session=f"{role}-watchdog",
        cmd=f"python -m services.watchdog.daemon --role {role}",
        capabilities=frozenset({role}),  # type: ignore[arg-type]
        requires_db=True,  # the watchdog's schema controller reads the DB
        pidfile=pidfile,
    )


# --- liveness -------------------------------------------------------------


def test_missing_pidfile_reads_as_dead(tmp_path: Path) -> None:
    assert wp._alive(_spec_for("gateway", tmp_path / "absent.pid")) is False


def test_garbage_pidfile_reads_as_dead(tmp_path: Path) -> None:
    """A truncated / half-written pidfile must not raise out of a scheduled job."""
    pid = tmp_path / "w.pid"
    pid.write_text("not-a-pid")
    assert wp._alive(_spec_for("gateway", pid)) is False


def test_live_pid_reads_as_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid = tmp_path / "w.pid"
    pid.write_text("4321")
    monkeypatch.setattr(wp, "process_alive", lambda p: p == 4321)  # pyright: ignore[reportUnknownArgumentType]
    assert wp._alive(_spec_for("gateway", pid)) is True


def test_spec_without_pidfile_is_a_hard_error(tmp_path: Path) -> None:
    """The watchdog specs all carry a pidfile; losing it would silently disable
    the probe, so fail loudly instead (repo principle 2: fail fast)."""
    with pytest.raises(ValueError, match="no pidfile"):
        wp._alive(_spec_for("gateway", None))


# --- roster lookup --------------------------------------------------------


def test_spec_comes_from_the_ops_roster(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Session name / cmd / pidfile are read from ops.spec, not duplicated here."""
    want = _spec_for("agent-runner", tmp_path / "ar.pid")
    monkeypatch.setattr(wp, "build_services", lambda: (_spec_for("gateway", None), want))
    assert wp._watchdog_spec("agent-runner") is want


def test_unknown_role_is_a_hard_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wp, "build_services", lambda: ())
    with pytest.raises(ValueError, match="ops roster"):
        wp._watchdog_spec("gateway")


# --- the probe decision ---------------------------------------------------


def test_live_watchdog_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case, once a minute, forever: do nothing."""
    spec = _spec_for("gateway", tmp_path / "w.pid")
    monkeypatch.setattr(wp, "_watchdog_spec", lambda _r: spec)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(wp, "_alive", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    respawns: list[str] = []
    monkeypatch.setattr(
        "shared.service_respawn.respawn_service",
        lambda s, _c, _r, **_k: respawns.append(s) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert wp.cmd_watchdog_probe("gateway") == 0
    assert respawns == []


def test_dead_watchdog_is_respawned_with_its_own_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec_for("agent-runner", tmp_path / "w.pid")
    monkeypatch.setattr(wp, "_watchdog_spec", lambda _r: spec)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(wp, "_alive", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    seen: dict[str, object] = {}

    def _respawn(session, cmd, repo, **_k):  # type: ignore[no-untyped-def]
        seen.update(session=session, cmd=cmd, repo=repo)  # pyright: ignore[reportUnknownArgumentType]
        return True

    monkeypatch.setattr("shared.service_respawn.respawn_service", _respawn)  # pyright: ignore[reportUnknownArgumentType]
    assert wp.cmd_watchdog_probe("agent-runner") == 0
    assert seen["session"] == "agent-runner-watchdog"
    assert "--role agent-runner" in str(seen["cmd"])


def test_respawn_is_forced_through_the_source_switch_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe revives the watchdog even while an update is mid-checkout
    (respawn_service's source-switch guard would otherwise hold it back): its
    contract is dumb revival that ignores every gate, and a watchdog that stays
    dead through the whole update leaves the capability unsupervised."""
    monkeypatch.setattr(
        wp,
        "_watchdog_spec",
        lambda _r: _spec_for("agent-runner", tmp_path / "w.pid"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(wp, "_alive", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    seen: dict[str, object] = {}

    def _respawn(session: str, cmd: str, repo: Path, **kwargs: object) -> bool:
        seen.update(session=session, force=kwargs.get("force"))
        return True

    monkeypatch.setattr("shared.service_respawn.respawn_service", _respawn)
    assert wp.cmd_watchdog_probe("agent-runner") == 0
    assert seen["force"] is True


def test_failed_respawn_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent 0 here would tell the operator the watchdog is fine while its
    capability's services stay down — the failure mode this whole feature exists
    to end."""
    monkeypatch.setattr(wp, "_watchdog_spec", lambda _r: _spec_for("gateway", tmp_path / "w.pid"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(wp, "_alive", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.service_respawn.respawn_service", lambda *_a, **_k: False)  # pyright: ignore[reportUnknownArgumentType]
    assert wp.cmd_watchdog_probe("gateway") == 1
