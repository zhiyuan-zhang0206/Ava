"""Unit tests for `shared/machines.py` — env/file precedence + lookup roundtrip + exception paths
+ composition (machine_units -> machines) semantics.

machines / machine_units schema uses the schema.sql at the top of conftest; here we only verify helper behavior.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from shared import machines
from shared.agents import MachineNotRegistered
from shared.config import settings


@pytest.fixture(autouse=True)
def _truncate_machines() -> None:
    """Truncate both tables per test — leftover register_self/recompute across tests would pollute subsequent assertions."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        cur.execute("TRUNCATE machine_units")
        conn.commit()


# ─── gateway_url() precedence ─────────────────────────────────────────────────


def test_gateway_url_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env `AVA_GATEWAY_URL` set > file — env wins."""
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://from-env:8000")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)
    (tmp_path / "gateway_url").write_text("http://from-file:8000")
    assert machines.gateway_url() == "http://from-env:8000"


def test_gateway_url_file_when_env_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env not set → reads `$AVA_HOME/gateway_url`."""
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)
    (tmp_path / "gateway_url").write_text("http://from-file:8000\n")  # trailing \n trimmed
    assert machines.gateway_url() == "http://from-file:8000"


def test_gateway_url_raises_when_neither(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env not set + file doesn't exist → GatewayUrlMissing."""
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)
    with pytest.raises(machines.GatewayUrlMissing):
        machines.gateway_url()


def test_gateway_url_raises_when_file_blank(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Blank file treated as not set — protects users who wrote echo "" without cat check."""
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)
    (tmp_path / "gateway_url").write_text("   \n")
    with pytest.raises(machines.GatewayUrlMissing):
        machines.gateway_url()


# ─── register_self + lookup roundtrip ─────────────────────────────────────────


@pytest.fixture
def _machine_setup(monkeypatch: pytest.MonkeyPatch):
    """Inject machine identity (name/role through _coerce_roles validation) + control home.

    register_self uses `shared.machines.ava_home()` to get the unit's home; by default fake a
    fixed home, single-box tests don't need to care. co-located tests use the returned `set_home` to switch to different
    home then register_self, simulating two units on the same machine name. teardown reset_identity() prevents injected values
    from leaking into subsequent test files.
    """
    from shared.machine import reset_identity, set_identity

    state = {"home": "~/.ava"}
    monkeypatch.setattr("shared.machines.ava_home", lambda: state["home"])
    # register_self's loopback guard reads the configured gateway URL to decide
    # whether a loopback ops URL is legal (co-located gateway) or a misconfig
    # (remote gateway). These fixtures model co-located units, so pin the gateway
    # URL to loopback — a loopback ops URL is then correctly accepted.
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://localhost:8000")

    def _set(*, name: str, role: str = "gateway", home: str = "~/.ava") -> None:
        set_identity(name=name, role=role)
        state["home"] = home

    _set.set_home = lambda home: state.__setitem__("home", home)  # type: ignore[attr-defined]
    yield _set
    reset_identity()


def _read_machine(name: str) -> tuple:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT gateway_url, role, description, stopped_at FROM machines WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def test_register_self_and_lookup_roundtrip(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """register_self UPSERT through machine_units -> recompute machines; lookup returns same URL."""
    _machine_setup(name="test-rt-machine")
    machines.register_self(url="http://rt:8000")
    assert machines.lookup("test-rt-machine") == "http://rt:8000"


def test_register_self_composes_single_unit_row(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Single unit (wsl style): composed machines row reflects that unit's caps + url."""
    _machine_setup(name="wsl", role="agent-runner")
    machines.register_self(url="http://wsl:9100")
    gateway_url, role, _desc, stopped_at = _read_machine("wsl")  # pyright: ignore[reportUnknownVariableType]
    assert gateway_url == "http://wsl:9100"
    assert role == ["agent-runner"]
    assert stopped_at is None


def test_register_self_overwrites_url(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Same unit second register_self overwrites URL (ON CONFLICT DO UPDATE)."""
    _machine_setup(name="test-overwrite")
    machines.register_self(url="http://old:8000")
    machines.register_self(url="http://new:8000")
    assert machines.lookup("test-overwrite") == "http://new:8000"


def test_register_self_agent_runner_stores_null(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """agent-runner: register_self(url=None) → composed gateway_url NULL."""
    _machine_setup(name="test-agent-runner", role="agent-runner")
    machines.register_self(url=None)
    with pytest.raises(machines.MachineGatewayUrlMissing):
        machines.lookup("test-agent-runner")


def _read_stopped_at(name: str):
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT stopped_at FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    return row[0] if row else None


def test_mark_stopping_stamps_then_register_clears(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Single unit host: mark_stopping(name, home) marks stopped (no live unit → machines
    stopped_at set); next register_self (comeback) clears back to NULL."""
    _machine_setup(name="test-stopping", role="agent-runner", home="~/.ava")
    machines.register_self(url=None)
    assert _read_stopped_at("test-stopping") is None

    machines.mark_stopping("test-stopping", "~/.ava")
    assert _read_stopped_at("test-stopping") is not None

    machines.register_self(url=None)
    assert _read_stopped_at("test-stopping") is None


def test_register_self_does_not_fall_back_to_gateway_url_when_url_none(
    _machine_setup,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """url=None does not use gateway_url() env/file fallback. Even if AVA_GATEWAY_URL is set,
    register_self(url=None) still writes NULL."""
    _machine_setup(name="test-no-fallback", role="agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://should-be-ignored:8000")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)
    machines.register_self(url=None)
    assert machines.list_all() == [("test-no-fallback", None)]


def test_lookup_raises_when_missing() -> None:
    """No corresponding row in machines table → MachineNotRegistered (wire-encoded, 404)."""
    with pytest.raises(MachineNotRegistered):
        machines.lookup("never-registered")


def test_list_all_returns_registered(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """list_all returns all (name, url-or-None) sorted by name."""
    _machine_setup(name="alpha")
    machines.register_self(url="http://a:8000")
    _machine_setup(name="beta", role="agent-runner")
    machines.register_self(url=None)
    assert machines.list_all() == [
        ("alpha", "http://a:8000"),
        ("beta", None),
    ]


def test_list_agent_runners_filters_by_role(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """list_agent_runners only returns (name, url) where composed role contains 'agent-runner'.
    gateway-only row not included."""
    _machine_setup(name="cp", role="gateway")
    machines.register_self(url="http://cp:8000")
    _machine_setup(name="host-b", role="agent-runner")
    machines.register_self(url="http://b:9000")
    _machine_setup(name="host-a", role="agent-runner")
    machines.register_self(url=None)
    assert machines.list_agent_runners() == [
        ("host-a", None),
        ("host-b", "http://b:9000"),
    ]


def test_list_agent_runners_includes_all_runners(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Every agent-runner row is returned — there is no deployment-scope branch."""
    _machine_setup(name="other-box", role="agent-runner")
    machines.register_self(url="http://other:9000")
    _machine_setup(name="this-box", role="agent-runner")
    machines.register_self(url="http://local:9000")

    assert machines.list_agent_runners() == [
        ("other-box", "http://other:9000"),
        ("this-box", "http://local:9000"),
    ]


def test_list_agent_runners_excludes_intentionally_stopped(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """A host that announced an intentional stop is not a rollout target; an
    unmarked host stays."""
    _machine_setup(name="running", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://running:9000")
    _machine_setup(name="stopped", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://stopped:9000")
    machines.mark_stopping("stopped", "~/.ava")

    assert machines.list_agent_runners() == [("running", "http://running:9000")]


def test_list_stopped_agent_runners_is_the_exact_complement(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """The excluded rows are enumerable, so a rollout can say "N of M" instead of a
    bare count of whatever survived the filter — the silent count is what hid the
    2026-07-28 exclusion."""
    _machine_setup(name="running", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://running:9000")
    _machine_setup(name="stopped", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://stopped:9000")
    machines.mark_stopping("stopped", "~/.ava")

    assert machines.list_stopped_agent_runners() == [("stopped", "http://stopped:9000")]
    # complement: the two lists partition the agent-runner rows, no overlap, no gap
    assert set(machines.list_agent_runners()) & set(machines.list_stopped_agent_runners()) == set()


def test_list_agent_runners_excludes_staging(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """A host operator-flagged staging is registered + visible but never a
    rollout target; the flag is independent of the stop latch (an `ava start`
    on it clears stopped_at and it STILL is not a fan-out target)."""
    _machine_setup(name="prod", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://prod:9000")
    _machine_setup(name="stage", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://stage:9000")

    assert machines.set_staging("stage", is_staging=True) is True
    assert machines.list_agent_runners() == [("prod", "http://prod:9000")]
    # staging row stays enumerable as stopped-complement? No — it is not the
    # stopped set either; the rollout's "N of M" counts only non-staging hosts.
    assert machines.list_stopped_agent_runners() == []

    # `ava start` on the staging host clears its stopped latch (register_self)
    # — the staging flag survives recompute and keeps it out of the fan-out.
    machines.register_self(url="http://stage:9000")
    assert machines.list_agent_runners() == [("prod", "http://prod:9000")]

    # unmark → back in the target set
    assert machines.set_staging("stage", is_staging=False) is True
    assert machines.list_agent_runners() == [
        ("prod", "http://prod:9000"),
        ("stage", "http://stage:9000"),
    ]


def test_set_staging_unknown_machine_returns_false(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """set_staging on a name with no row is a no-op reported as False (the CLI
    turns it into a 404-style error)."""
    assert machines.set_staging("ghost", is_staging=True) is False


def _read_pause(name: str) -> tuple:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT paused_at, pause_reason FROM machines WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def test_pause_sets_latch_and_excludes_from_fanout(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """`pause` stamps paused_at + pause_reason; the row drops out of
    list_agent_runners (probe + rollout skip it) and is enumerable via
    list_paused. list_stopped is unaffected — pause is not a stop."""
    _machine_setup(name="prod", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://prod:9000")
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://away:9000")

    assert machines.pause("away", reason="休假一周") is True
    paused_at, pause_reason = _read_pause("away")
    assert paused_at is not None
    assert pause_reason == "休假一周"
    assert machines.list_agent_runners() == [("prod", "http://prod:9000")]
    assert machines.list_paused() == [("away", "http://away:9000")]
    # the stop complement is untouched — a paused machine is neither a rollout
    # target nor a "stopped" row
    assert machines.list_stopped_agent_runners() == []


def test_register_self_does_not_clear_pause(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """THE pause invariant: a paused machine that re-registers (its `ava start`
    after a reboot, or a reachable-address change while it is away) stays paused —
    only `resume` clears the latch. register_self still clears the unit's
    stopped_at latch (normal comeback semantics) and refreshes the URL."""
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://away:9000")
    machines.pause("away", reason="away")

    machines.register_self(url="http://away-new-ip:9000")

    paused_at, pause_reason = _read_pause("away")
    assert paused_at is not None  # still paused
    assert pause_reason == "away"
    assert machines.list_agent_runners() == []  # still excluded
    assert machines.lookup("away") == "http://away-new-ip:9000"  # URL refreshed


def test_resume_clears_latch_and_restores_fanout(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """`resume` clears paused_at + pause_reason; the row is a normal rollout
    target again and list_paused is empty."""
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://away:9000")
    machines.pause("away", reason="away")

    assert machines.resume("away") is True
    assert _read_pause("away") == (None, None)
    assert machines.list_agent_runners() == [("away", "http://away:9000")]
    assert machines.list_paused() == []


def test_pause_resume_idempotency(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """pause of an already-paused row and resume of a not-paused row are
    no-ops reported as False (the CLI turns that into a message, not an
    error) — a re-run of a partially-failed pause is safe."""
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://away:9000")

    assert machines.pause("away") is True
    assert machines.pause("away", reason="second attempt") is False
    # the original reason is preserved — the second call changed nothing
    assert _read_pause("away")[1] is None

    assert machines.resume("away") is True
    assert machines.resume("away") is False


def test_pause_unknown_machine_returns_false(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """pause/resume on a name with no row are no-ops reported as False."""
    assert machines.pause("ghost", reason="x") is False
    assert machines.resume("ghost") is False


def test_is_paused_reads_the_latch(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """is_paused: True while the latch is set, False after resume, raises
    MachineNotRegistered for an unknown name (same contract as lookup_role)."""
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://away:9000")

    assert machines.is_paused("away") is False
    machines.pause("away")
    assert machines.is_paused("away") is True
    machines.resume("away")
    assert machines.is_paused("away") is False
    with pytest.raises(MachineNotRegistered):
        machines.is_paused("never-registered")


def test_pause_independent_of_staging_and_stop(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """The three exclusions compose: a paused row is out even if its staging
    flag is cleared later, and a stopped+paused row is out of both lists."""
    _machine_setup(name="prod", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://prod:9000")
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://away:9000")

    machines.set_staging("away", is_staging=True)
    machines.pause("away")
    assert machines.list_agent_runners() == [("prod", "http://prod:9000")]
    # unmarking staging does NOT restore a paused machine
    machines.set_staging("away", is_staging=False)
    assert machines.list_agent_runners() == [("prod", "http://prod:9000")]
    # and the pause is not a stop: list_stopped stays empty even while paused
    assert machines.list_stopped_agent_runners() == []
    machines.resume("away")
    assert machines.list_agent_runners() == [
        ("away", "http://away:9000"),
        ("prod", "http://prod:9000"),
    ]


def test_clear_stopped_marker_puts_a_stale_row_back_in_the_fan_out(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """A probe proving the host live outranks the stop latch: clearing the composed
    row's marker is what makes the roster and the next fan-out agree again."""
    _machine_setup(name="stale", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://stale:9000")
    machines.mark_stopping("stale", "~/.ava")
    assert machines.list_agent_runners() == []

    assert machines.clear_stopped_marker("stale") is True
    assert machines.list_agent_runners() == [("stale", "http://stale:9000")]
    assert machines.list_stopped_agent_runners() == []
    # idempotent: a second reconcile of an already-clear row changes nothing
    assert machines.clear_stopped_marker("stale") is False


# ─── unit_dial_url() — the one address definition both writers share ─────────


def test_unit_dial_url_single_box_is_loopback_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """gateway + agent-runner on a true single box: `reachable_host()` resolves
    to localhost, so the advertised ops URL is loopback (single box needs no
    reachable address)."""
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "localhost")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8600 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    assert machines.unit_dial_url(frozenset({"gateway", "agent-runner"})) == "http://localhost:8600"


def test_unit_dial_url_gateway_runner_uses_reachable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gateway + agent-runner on a machine with a reachable identity: the unit
    advertises `reachable_host()`, not unconditional localhost — otherwise the
    page proxy's SSRF guard (which only dials registered machine addresses)
    rejects every page registration from agents on the gateway box
    (2026-08-12 serve outage)."""
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "100.64.0.2")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8600 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    assert (
        machines.unit_dial_url(frozenset({"gateway", "agent-runner"})) == "http://100.64.0.2:8600"
    )


def test_unit_dial_url_split_runner_is_reachable_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent-runner only: the remote gateway must reach it, so the URL carries
    `reachable_host()`, not loopback."""
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "100.64.0.2")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8600 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    assert machines.unit_dial_url(frozenset({"agent-runner"})) == "http://100.64.0.2:8600"


def test_unit_dial_url_gateway_only_is_the_gateway_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gateway only: no ops server runs, so it advertises its own gateway URL."""
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)
    (tmp_path / "gateway_url").write_text("https://ava.example:8000")
    assert machines.unit_dial_url(frozenset({"gateway"})) == "https://ava.example:8000"


def test_unit_dial_url_agrees_across_both_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava start` and the ops daemon's boot registration compute this address from
    the SAME function, so a unit cannot be advertised at two addresses depending on
    which one wrote last. Asserted on the identical capability set both pass.
    """
    from shared.machine import machine_role, reset_identity, set_identity

    monkeypatch.setattr("shared.machine.reachable_host", lambda: "100.64.0.7")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8600 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    set_identity(name="split-runner", role="agent-runner")
    try:
        # `ava start` passes its resolved capability set; the daemon passes machine_role().
        from_start = machines.unit_dial_url(frozenset({"agent-runner"}))
        from_daemon = machines.unit_dial_url(machine_role())
    finally:
        reset_identity()
    assert from_start == from_daemon == "http://100.64.0.7:8600"


# ─── co-located compose + downgrade ──────────────────────────────────────────


def test_colocated_units_compose_union_and_ops_dial(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Two co-located units on the same machine name (gateway-only @ ~/.ava_gateway + agent-runner @
    ~/.ava) compose into ONE machines row: role = union; gateway_url = ops URL
    (agent-runner unit's url, because the host serves agent-runner)."""
    # gateway-only unit
    _machine_setup(name="test-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(url="http://test-host:8000")
    # agent-runner-only unit (same machine name, different home)
    _machine_setup(name="test-host", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://localhost:8600")

    gateway_url, role, _desc, stopped_at = _read_machine("test-host")  # pyright: ignore[reportUnknownVariableType]
    assert role == ["agent-runner", "gateway"]  # sorted union
    assert gateway_url == "http://localhost:8600"  # ops URL of the agent-runner unit
    assert stopped_at is None
    # exactly one machines row, two machine_units rows
    assert machines.list_all() == [("test-host", "http://localhost:8600")]


def test_colocated_stop_one_unit_retracts_only_its_caps(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Stopping the agent-runner unit on a co-located host: composed role downgrades to gateway only,
    gateway_url switches back to the gateway unit's url; host still live (gateway unit running)."""
    _machine_setup(name="test-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(url="http://test-host:8000")
    _machine_setup(name="test-host", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://localhost:8600")

    machines.mark_stopping("test-host", "~/.ava")  # stop the agent-runner unit

    gateway_url, role, _desc, stopped_at = _read_machine("test-host")  # pyright: ignore[reportUnknownVariableType]
    assert role == ["gateway"]  # agent-runner cap retracted
    assert gateway_url == "http://test-host:8000"  # now the gateway unit's url
    assert stopped_at is None  # gateway unit still live
    # the agent-runner unit dropped out of the fan-out target list
    assert machines.list_agent_runners() == []


def test_colocated_restart_unit_recomposes_union(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Stopped unit re-register_self comes back: composed role again includes agent-runner."""
    _machine_setup(name="test-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(url="http://test-host:8000")
    _machine_setup(name="test-host", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://localhost:8600")
    machines.mark_stopping("test-host", "~/.ava")

    _machine_setup(name="test-host", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://localhost:8600")

    _gateway_url, role, _desc, _stopped = _read_machine("test-host")  # pyright: ignore[reportUnknownVariableType]
    assert role == ["agent-runner", "gateway"]


# ─── register_self loopback dial-URL guard ───────────────────────────────────


def test_register_self_rejects_loopback_ops_url_when_gateway_remote(
    _machine_setup,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """An agent-runner-only unit whose gateway is REMOTE must not register a
    loopback ops URL — the gateway would dial itself (the 2026-07-18 incident).
    Raised before any DB write."""
    _machine_setup(name="wsl", role="agent-runner")
    monkeypatch.setattr(
        settings.gateway, "gateway_url", "https://gw.example.com:8000"
    )  # remote gateway
    with pytest.raises(machines.LoopbackDialUrlRefused):
        machines.register_self(url="http://localhost:8600")
    # nothing landed in the table
    assert machines.list_all() == []


def test_register_self_allows_loopback_ops_url_when_gateway_colocated(
    _machine_setup,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """Same loopback ops URL is LEGAL when the gateway is co-located (loopback
    gateway URL) — a split-home single box dials its runner over loopback."""
    _machine_setup(name="box", role="agent-runner")
    monkeypatch.setattr(
        settings.gateway, "gateway_url", "http://localhost:8000"
    )  # co-located gateway
    machines.register_self(url="http://localhost:8600")
    assert machines.list_all() == [("box", "http://localhost:8600")]


def test_register_self_allows_loopback_ops_url_when_also_gateway(
    _machine_setup,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """A single co-located unit that serves BOTH gateway and agent-runner may
    register a loopback ops URL regardless of its gateway URL — its own gateway
    self-dials over loopback."""
    _machine_setup(name="single", role="gateway,agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://gw.example.com:8000")
    machines.register_self(url="http://localhost:8600")
    assert machines.list_all() == [("single", "http://localhost:8600")]


# ─── register_self description column ────────────────────────────────────────


def _read_description(name: str) -> str | None:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT description FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_register_self_writes_description(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """register_self picks up machine_description() and UPSERTs it."""
    from shared.machine import set_identity

    _machine_setup(name="desc-machine")
    set_identity(description="voice IO + browser")
    machines.register_self(url="http://d:8000")
    assert _read_description("desc-machine") == "voice IO + browser"


def test_register_self_description_none_stores_null(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """No description configured → column NULL."""
    from shared.machine import set_identity

    _machine_setup(name="nodesc-machine")
    set_identity(description=None)
    machines.register_self(url="http://n:8000")
    assert _read_description("nodesc-machine") is None


def test_register_self_updates_description_on_conflict(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """Second register_self overwrites description (ON CONFLICT DO UPDATE)."""
    from shared.machine import set_identity

    _machine_setup(name="upd-machine")
    set_identity(description="old")
    machines.register_self(url="http://u:8000")
    set_identity(description="new")
    machines.register_self(url="http://u:8000")
    assert _read_description("upd-machine") == "new"


def test_mark_stopping_preserves_description(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """stop-triggered recompute does not touch description (host-level, only register writes it)."""
    from shared.machine import set_identity

    _machine_setup(name="keepdesc", role="agent-runner", home="~/.ava")
    set_identity(description="keep me")
    machines.register_self(url="http://k:9000")
    machines.mark_stopping("keepdesc", "~/.ava")
    assert _read_description("keepdesc") == "keep me"


# ─── up_since_at — the announce stamp (#981) ─────────────────────────────────


def _read_up_since(name: str):
    """machines.up_since_at for `name`."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT up_since_at FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    assert row is not None
    return row


def _read_unit_up_since(name: str, home: str):
    """machine_units.up_since_at for one unit."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT up_since_at FROM machine_units WHERE machine_name = %s AND home = %s",
            (name, home),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def test_register_self_stamps_up_since_on_unit_and_composed_row(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """register_self stamps the announce time on the unit row, and the recompute
    carries it onto the composed machines row.

    The column is the "up since" the CLI and the status page render — it exists to
    be shown, so what it must survive is exactly this write-then-compose path.
    """
    _machine_setup(name="stamp-host", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://stamp-host:9100")

    (unit_up_since,) = _read_unit_up_since("stamp-host", "~/.ava")
    (composed_up_since,) = _read_up_since("stamp-host")
    assert unit_up_since is not None
    assert composed_up_since == unit_up_since


def test_composed_up_since_is_the_max_over_live_units(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """The composed row takes the LATEST announce across a machine's live units.

    Two co-located units announce at different times; the machine has been up
    since the later one, because that is the one whose announcement is still the
    most recent claim about this host.
    """
    _machine_setup(name="max-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(url="http://max-host:8000")
    _machine_setup(name="max-host", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://localhost:8600")

    (gateway_unit,) = _read_unit_up_since("max-host", "~/.ava_gateway")
    (runner_unit,) = _read_unit_up_since("max-host", "~/.ava")
    (composed,) = _read_up_since("max-host")
    assert composed == max(gateway_unit, runner_unit)


def test_recompute_tolerates_a_unit_with_null_up_since(_machine_setup) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """A unit row whose `up_since_at` is NULL still composes.

    `up_since_at` is nullable on machine_units (a unit registered before #981
    has no stamp — the expand migration backfilled it from `last_seen_at`, and
    the contract migration dropped that column). The composition takes a max
    across units, so an unhandled NULL there would not degrade the display — it
    would raise and take the whole register_self down.
    """
    _machine_setup(name="skew-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(url="http://skew-host:8000")
    # Rewrite that unit the way a pre-#981 writer would have left it.
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machine_units SET up_since_at = NULL WHERE machine_name = %s AND home = %s",
            ("skew-host", "~/.ava_gateway"),
        )
        conn.commit()

    _machine_setup(name="skew-host", role="agent-runner", home="~/.ava")
    machines.register_self(url="http://localhost:8600")

    (runner_unit,) = _read_unit_up_since("skew-host", "~/.ava")
    (composed,) = _read_up_since("skew-host")
    # The fresh unit's stamp wins the max (the old unit contributes NULL).
    assert composed == runner_unit
    _gateway_url, role, _desc, _stopped = _read_machine("skew-host")  # pyright: ignore[reportUnknownVariableType]
    assert role == ["agent-runner", "gateway"]
