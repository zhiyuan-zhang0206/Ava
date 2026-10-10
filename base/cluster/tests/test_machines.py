"""Unit tests for `base/cluster/machines.py` — env/file precedence + lookup roundtrip + exception paths
+ composition (machine_units -> machines) semantics.

machines / machine_units schema uses the schema.sql at the top of conftest; here we only verify helper behavior.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from base.agents import MachineNotRegistered
from base.cluster import machines
from base.config import settings
from base.daemon.tests.fakes import pin_endpoints
from base.db import Database
from base.db.code_version_gate import ProcessDbGate


def _db(database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


@pytest.fixture(autouse=True)
def _truncate_machines() -> None:
    """Truncate both tables per test — leftover register_self/recompute across tests would pollute subsequent assertions."""
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        cur.execute("TRUNCATE machine_units")
        conn.commit()


# gateway_url() source
def test_gateway_url_comes_from_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`AVA_GATEWAY_URL` is the only source: a `gateway_url` file in the home is not read."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    (tmp_path / "gateway_url").write_text("http://from-file:8000")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://from-env:8000/")
    assert machines.gateway_url() == "http://from-env:8000"
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    with pytest.raises(machines.GatewayUrlMissing):
        machines.gateway_url()


def test_gateway_url_raises_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank value is not set."""
    monkeypatch.setattr(settings.gateway, "gateway_url", "   ")
    with pytest.raises(machines.GatewayUrlMissing):
        machines.gateway_url()


# register_self + lookup roundtrip
@pytest.fixture
def _machine_setup(monkeypatch: pytest.MonkeyPatch):
    """Inject machine identity (name/role through _coerce_roles validation) + control home.

    register_self uses `base.cluster.machines.ava_home()` to get the unit's home; by default fake a
    fixed home, single-box tests don't need to care. co-located tests use the returned `set_home` to switch to different
    home then register_self, simulating two units on the same machine name. teardown reset_identity() prevents injected values
    from leaking into subsequent test files.
    """
    from base.cluster.machine import reset_identity, set_identity

    state = {"home": "~/.ava"}
    monkeypatch.setattr("base.cluster.machines.ava_home", lambda: state["home"])
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


def _read_machine(name: str) -> tuple:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT gateway_url, role, description, stopped_at FROM machines WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def test_register_self_and_lookup_roundtrip(_machine_setup, database_gate: ProcessDbGate) -> None:
    """register_self UPSERT through machine_units -> recompute machines; lookup returns same URL."""
    _machine_setup(name="test-rt-machine")
    machines.register_self(_db(database_gate=database_gate), url="http://rt:8000")
    assert machines.lookup(_db(database_gate=database_gate), "test-rt-machine") == "http://rt:8000"


def test_register_self_composes_single_unit_row(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """Single unit (wsl style): composed machines row reflects that unit's caps + url."""
    _machine_setup(name="wsl", role="agent-runner")
    machines.register_self(_db(database_gate=database_gate), url="http://wsl:9100")
    gateway_url, role, _desc, stopped_at = _read_machine("wsl")
    assert gateway_url == "http://wsl:9100"
    assert role == ["agent-runner"]
    assert stopped_at is None


def test_register_self_overwrites_url(_machine_setup, database_gate: ProcessDbGate) -> None:
    """Same unit second register_self overwrites URL (ON CONFLICT DO UPDATE)."""
    _machine_setup(name="test-overwrite")
    machines.register_self(_db(database_gate=database_gate), url="http://old:8000")
    machines.register_self(_db(database_gate=database_gate), url="http://new:8000")
    assert machines.lookup(_db(database_gate=database_gate), "test-overwrite") == "http://new:8000"


def test_register_self_agent_runner_stores_null(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """agent-runner: register_self(_db(), url=None) → composed gateway_url NULL."""
    _machine_setup(name="test-agent-runner", role="agent-runner")
    machines.register_self(_db(database_gate=database_gate), url=None)
    with pytest.raises(machines.MachineGatewayUrlMissing):
        machines.lookup(_db(database_gate=database_gate), "test-agent-runner")


def test_register_self_station_only_composes_row(
    _machine_setup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """A pure observability-station unit (neither gateway nor agent-runner) must
    compose a machines row carrying its capability — otherwise the station host
    is invisible to `ava cluster status`. The composed row advertises the
    station's OTLP ingress URL (its reachable-host dial target, WP4) and
    stopped_at NULL (the unit is live)."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.9")
    _machine_setup(name="station-a", role="observability-station")
    machines.register_self(
        _db(database_gate=database_gate),
        url=machines.unit_dial_url(frozenset({"observability-station"})),
    )
    gateway_url, role, _desc, stopped_at = _read_machine("station-a")
    assert gateway_url == "http://10.0.0.9:4318"
    assert role == ["observability-station"]
    assert stopped_at is None


def test_register_self_co_located_station_composes_with_gateway(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """Two co-located units (a gateway unit + a station unit on the same host)
    compose into ONE machines row whose role is the union — the station
    capability joins the gateway's, exactly like the gateway/agent-runner
    composition."""
    _machine_setup(name="combo", role="gateway", home="~/.ava_gateway")
    machines.register_self(_db(database_gate=database_gate), url="http://gw:8000")
    _machine_setup(name="combo", role="observability-station", home="~/.ava_station")
    machines.register_self(_db(database_gate=database_gate), url=None)
    gateway_url, role, _desc, stopped_at = _read_machine("combo")
    assert role == ["gateway", "observability-station"]
    # the composed dial URL stays the gateway unit's (the station adds no target)
    assert gateway_url == "http://gw:8000"
    assert stopped_at is None


def test_unit_dial_url_pure_station_is_otlp_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure station advertises its bearer-authenticated OTLP ingress — the
    one station address remote consumers dial — derived from reachable_host
    and AVA_TELEMETRY_OTLP_PORT (single source, task #1945). No gateway URL is
    required (a station host does not have one)."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.9")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    assert machines.unit_dial_url(frozenset({"observability-station"})) == ("http://10.0.0.9:4318")


def test_unit_dial_url_gateway_station_is_reachable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gateway + station (no runner) advertises the reachable-host form of the
    gateway URL — the station capability must not change the gateway unit's
    advertised address, but the host is always reachable_host(), never the
    bare gateway URL (WP4: a loopback advertisement makes the page proxy
    refuse the host's page servers)."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://gw:8000")
    assert machines.unit_dial_url(frozenset({"gateway", "observability-station"})) == (
        "http://10.0.0.5:8000"
    )


def test_unit_dial_url_runner_station_is_ops_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent-runner + station keeps the ops-URL dial — the station capability
    must not change the runner unit's advertised address."""
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://gw:8000")
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.5")
    url = machines.unit_dial_url(frozenset({"agent-runner", "observability-station"}))
    assert url is not None and url.startswith("http://10.0.0.5:")


def _read_stopped_at(name: str):
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT stopped_at FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    return row[0] if row else None


def test_mark_stopping_stamps_then_register_clears(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """Single unit host: mark_stopping(_db(), name, home) marks stopped (no live unit → machines
    stopped_at set); next register_self (comeback) clears back to NULL."""
    _machine_setup(name="test-stopping", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url=None)
    assert _read_stopped_at("test-stopping") is None

    machines.mark_stopping(_db(database_gate=database_gate), "test-stopping", "~/.ava")
    assert _read_stopped_at("test-stopping") is not None

    machines.register_self(_db(database_gate=database_gate), url=None)
    assert _read_stopped_at("test-stopping") is None


def test_register_self_does_not_fall_back_to_gateway_url_when_url_none(
    _machine_setup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """url=None does not use the gateway_url() fallback. Even if AVA_GATEWAY_URL is set,
    register_self(_db(), url=None) still writes NULL."""
    _machine_setup(name="test-no-fallback", role="agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://should-be-ignored:8000")
    machines.register_self(_db(database_gate=database_gate), url=None)
    assert machines.list_all(_db(database_gate=database_gate)) == [("test-no-fallback", None)]


def test_lookup_raises_when_missing(database_gate: ProcessDbGate) -> None:
    """No corresponding row in machines table → MachineNotRegistered (wire-encoded, 404)."""
    with pytest.raises(MachineNotRegistered):
        machines.lookup(_db(database_gate=database_gate), "never-registered")


def test_list_all_returns_registered(_machine_setup, database_gate: ProcessDbGate) -> None:
    """list_all returns all (name, url-or-None) sorted by name."""
    _machine_setup(name="alpha")
    machines.register_self(_db(database_gate=database_gate), url="http://a:8000")
    _machine_setup(name="beta", role="agent-runner")
    machines.register_self(_db(database_gate=database_gate), url=None)
    assert machines.list_all(_db(database_gate=database_gate)) == [
        ("alpha", "http://a:8000"),
        ("beta", None),
    ]


def test_list_agent_runners_filters_by_role(_machine_setup, database_gate: ProcessDbGate) -> None:
    """list_agent_runners only returns (name, url) where composed role contains 'agent-runner'.
    gateway-only row not included."""
    _machine_setup(name="cp", role="gateway")
    machines.register_self(_db(database_gate=database_gate), url="http://cp:8000")
    _machine_setup(name="host-b", role="agent-runner")
    machines.register_self(_db(database_gate=database_gate), url="http://b:9000")
    _machine_setup(name="host-a", role="agent-runner")
    machines.register_self(_db(database_gate=database_gate), url=None)
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("host-a", None),
        ("host-b", "http://b:9000"),
    ]


def test_list_agent_runners_includes_all_runners(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """Every agent-runner row is returned — there is no deployment-scope branch."""
    _machine_setup(name="other-box", role="agent-runner")
    machines.register_self(_db(database_gate=database_gate), url="http://other:9000")
    _machine_setup(name="this-box", role="agent-runner")
    machines.register_self(_db(database_gate=database_gate), url="http://local:9000")

    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("other-box", "http://other:9000"),
        ("this-box", "http://local:9000"),
    ]


def test_list_agent_runners_excludes_intentionally_stopped(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """A host that announced an intentional stop is not a rollout target; an
    unmarked host stays."""
    _machine_setup(name="running", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://running:9000")
    _machine_setup(name="stopped", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://stopped:9000")
    machines.mark_stopping(_db(database_gate=database_gate), "stopped", "~/.ava")

    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("running", "http://running:9000")
    ]


def test_list_stopped_agent_runners_is_the_exact_complement(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """The excluded rows are enumerable, so a rollout can say "N of M" instead of a
    bare count of whatever survived the filter — the silent count is what hid the
    2026-07-28 exclusion."""
    _machine_setup(name="running", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://running:9000")
    _machine_setup(name="stopped", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://stopped:9000")
    machines.mark_stopping(_db(database_gate=database_gate), "stopped", "~/.ava")

    assert machines.list_stopped_agent_runners(_db(database_gate=database_gate)) == [
        ("stopped", "http://stopped:9000")
    ]
    # complement: the two lists partition the agent-runner rows, no overlap, no gap
    assert (
        set(machines.list_agent_runners(_db(database_gate=database_gate)))
        & set(machines.list_stopped_agent_runners(_db(database_gate=database_gate)))
        == set()
    )


def test_list_agent_runners_excludes_staging(_machine_setup, database_gate: ProcessDbGate) -> None:
    """A host operator-flagged staging is registered + visible but never a
    rollout target; the flag is independent of the stop latch (an `ava start`
    on it clears stopped_at and it STILL is not a fan-out target)."""
    _machine_setup(name="prod", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://prod:9000")
    _machine_setup(name="stage", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://stage:9000")

    assert machines.set_staging(_db(database_gate=database_gate), "stage", is_staging=True) is True
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]
    # staging row stays enumerable as stopped-complement? No — it is not the
    # stopped set either; the rollout's "N of M" counts only non-staging hosts.
    assert machines.list_stopped_agent_runners(_db(database_gate=database_gate)) == []

    # `ava start` on the staging host clears its stopped latch (register_self)
    # — the staging flag survives recompute and keeps it out of the fan-out.
    machines.register_self(_db(database_gate=database_gate), url="http://stage:9000")
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]

    # unmark → back in the target set
    assert machines.set_staging(_db(database_gate=database_gate), "stage", is_staging=False) is True
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000"),
        ("stage", "http://stage:9000"),
    ]


def test_set_staging_unknown_machine_returns_false(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """set_staging on a name with no row is a no-op reported as False (the CLI
    turns it into a 404-style error)."""
    assert machines.set_staging(_db(database_gate=database_gate), "ghost", is_staging=True) is False


def _read_pause(name: str) -> tuple:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT paused_at, pause_reason FROM machines WHERE name = %s",
            (name,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def test_pause_sets_latch_and_excludes_from_fanout(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """`pause` stamps paused_at + pause_reason; the row drops out of
    list_agent_runners (probe + rollout skip it) and is enumerable via
    list_paused. list_stopped is unaffected — pause is not a stop."""
    _machine_setup(name="prod", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://prod:9000")
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://away:9000")

    assert (
        machines.pause(_db(database_gate=database_gate), "away", reason="\u4f11\u5047\u4e00\u5468")
        is True
    )
    paused_at, pause_reason = _read_pause("away")
    assert paused_at is not None
    assert pause_reason == "\u4f11\u5047\u4e00\u5468"
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]
    assert machines.list_paused(_db(database_gate=database_gate)) == [("away", "http://away:9000")]
    # the stop complement is untouched — a paused machine is neither a rollout
    # target nor a "stopped" row
    assert machines.list_stopped_agent_runners(_db(database_gate=database_gate)) == []


def test_register_self_does_not_clear_pause(_machine_setup, database_gate: ProcessDbGate) -> None:
    """THE pause invariant: a paused machine that re-registers (its `ava start`
    after a reboot, or a reachable-address change while it is away) stays paused —
    only `resume` clears the latch. register_self still clears the unit's
    stopped_at latch (normal comeback semantics) and refreshes the URL."""
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://away:9000")
    machines.pause(_db(database_gate=database_gate), "away", reason="away")

    machines.register_self(_db(database_gate=database_gate), url="http://away-new-ip:9000")

    paused_at, pause_reason = _read_pause("away")
    assert paused_at is not None  # still paused
    assert pause_reason == "away"
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == []  # still excluded
    assert (
        machines.lookup(_db(database_gate=database_gate), "away") == "http://away-new-ip:9000"
    )  # URL refreshed


def test_resume_clears_latch_and_restores_fanout(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """`resume` clears paused_at + pause_reason; the row is a normal rollout
    target again and list_paused is empty."""
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://away:9000")
    machines.pause(_db(database_gate=database_gate), "away", reason="away")

    assert machines.resume(_db(database_gate=database_gate), "away") is True
    assert _read_pause("away") == (None, None)
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("away", "http://away:9000")
    ]
    assert machines.list_paused(_db(database_gate=database_gate)) == []


def test_pause_resume_idempotency(_machine_setup, database_gate: ProcessDbGate) -> None:
    """pause of an already-paused row and resume of a not-paused row are
    no-ops reported as False (the CLI turns that into a message, not an
    error) — a re-run of a partially-failed pause is safe."""
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://away:9000")

    assert machines.pause(_db(database_gate=database_gate), "away") is True
    assert (
        machines.pause(_db(database_gate=database_gate), "away", reason="second attempt") is False
    )
    # the original reason is preserved — the second call changed nothing
    assert _read_pause("away")[1] is None

    assert machines.resume(_db(database_gate=database_gate), "away") is True
    assert machines.resume(_db(database_gate=database_gate), "away") is False


def test_pause_unknown_machine_returns_false(_machine_setup, database_gate: ProcessDbGate) -> None:
    """pause/resume on a name with no row are no-ops reported as False."""
    assert machines.pause(_db(database_gate=database_gate), "ghost", reason="x") is False
    assert machines.resume(_db(database_gate=database_gate), "ghost") is False


def test_is_paused_reads_the_latch(_machine_setup, database_gate: ProcessDbGate) -> None:
    """is_paused: True while the latch is set, False after resume, raises
    MachineNotRegistered for an unknown name (same contract as lookup_role)."""
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://away:9000")

    assert machines.is_paused(_db(database_gate=database_gate), "away") is False
    machines.pause(_db(database_gate=database_gate), "away")
    assert machines.is_paused(_db(database_gate=database_gate), "away") is True
    machines.resume(_db(database_gate=database_gate), "away")
    assert machines.is_paused(_db(database_gate=database_gate), "away") is False
    with pytest.raises(MachineNotRegistered):
        machines.is_paused(_db(database_gate=database_gate), "never-registered")


def test_pause_independent_of_staging_and_stop(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """The three exclusions compose: a paused row is out even if its staging
    flag is cleared later, and a stopped+paused row is out of both lists."""
    _machine_setup(name="prod", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://prod:9000")
    _machine_setup(name="away", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://away:9000")

    machines.set_staging(_db(database_gate=database_gate), "away", is_staging=True)
    machines.pause(_db(database_gate=database_gate), "away")
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]
    # unmarking staging does NOT restore a paused machine
    machines.set_staging(_db(database_gate=database_gate), "away", is_staging=False)
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("prod", "http://prod:9000")
    ]
    # and the pause is not a stop: list_stopped stays empty even while paused
    assert machines.list_stopped_agent_runners(_db(database_gate=database_gate)) == []
    machines.resume(_db(database_gate=database_gate), "away")
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("away", "http://away:9000"),
        ("prod", "http://prod:9000"),
    ]


def test_clear_stopped_marker_puts_a_stale_row_back_in_the_fan_out(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """A probe proving the host live outranks the stop latch: clearing the composed
    row's marker is what makes the roster and the next fan-out agree again."""
    _machine_setup(name="stale", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://stale:9000")
    machines.mark_stopping(_db(database_gate=database_gate), "stale", "~/.ava")
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == []

    assert machines.clear_stopped_marker(_db(database_gate=database_gate), "stale") is True
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == [
        ("stale", "http://stale:9000")
    ]
    assert machines.list_stopped_agent_runners(_db(database_gate=database_gate)) == []
    # idempotent: a second reconcile of an already-clear row changes nothing
    assert machines.clear_stopped_marker(_db(database_gate=database_gate), "stale") is False


# unit_dial_url() — the one address definition both writers share
def test_unit_dial_url_single_box_is_loopback_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """gateway + agent-runner on a true single box: `reachable_host()` resolves
    to localhost, so the advertised ops URL is loopback (single box needs no
    reachable address)."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "localhost")
    pin_endpoints(monkeypatch, port=lambda name: 8600 if name == "ops" else 0)
    assert machines.unit_dial_url(frozenset({"gateway", "agent-runner"})) == "http://localhost:8600"


def test_unit_dial_url_gateway_runner_uses_reachable_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gateway + agent-runner on a machine with a reachable identity: the unit
    advertises `reachable_host()`, not unconditional localhost — otherwise the
    page proxy's SSRF guard (which only dials registered machine addresses)
    rejects every page registration from agents on the gateway box
    (2026-08-12 serve outage)."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.2")
    pin_endpoints(monkeypatch, port=lambda name: 8600 if name == "ops" else 0)
    assert machines.unit_dial_url(frozenset({"gateway", "agent-runner"})) == "http://10.0.0.2:8600"


def test_unit_dial_url_split_runner_is_reachable_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent-runner only: the remote gateway must reach it, so the URL carries
    `reachable_host()`, not loopback."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.2")
    pin_endpoints(monkeypatch, port=lambda name: 8600 if name == "ops" else 0)
    assert machines.unit_dial_url(frozenset({"agent-runner"})) == "http://10.0.0.2:8600"


def test_unit_dial_url_gateway_only_is_reachable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """gateway-only advertises reachable_host + the gateway URL's port, NOT the
    bare gateway URL — a gateway_url naming loopback on a host with a
    reachable identity would advertise a self-dialing address that breaks
    page serves (WP4, 2026-08-30 serve 400)."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://ava.example:8000")
    assert machines.unit_dial_url(frozenset({"gateway"})) == "http://10.0.0.2:8000"


def test_unit_dial_url_gateway_only_defaults_port_when_url_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gateway-only with no resolvable gateway URL still advertises the
    reachable host, on the gateway bind-port setting — the advertisement no
    longer depends on gateway_url being configured (the 400-scenario fix)."""
    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr(settings.gateway, "gateway_port", 8000)
    assert machines.unit_dial_url(frozenset({"gateway"})) == "http://10.0.0.2:8000"


def test_unit_dial_url_agrees_across_both_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava start` and the ops daemon's boot registration compute this address from
    the SAME function, so a unit cannot be advertised at two addresses depending on
    which one wrote last. Asserted on the identical capability set both pass.
    """
    from base.cluster.machine import machine_role, reset_identity, set_identity

    monkeypatch.setattr("base.cluster.machine.reachable_host", lambda: "10.0.0.7")
    pin_endpoints(monkeypatch, port=lambda name: 8600 if name == "ops" else 0)
    set_identity(name="split-runner", role="agent-runner")
    try:
        # `ava start` passes its resolved capability set; the daemon passes machine_role().
        from_start = machines.unit_dial_url(frozenset({"agent-runner"}))
        from_daemon = machines.unit_dial_url(machine_role())
    finally:
        reset_identity()
    assert from_start == from_daemon == "http://10.0.0.7:8600"


# co-located compose + downgrade
def test_colocated_units_compose_union_and_ops_dial(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """Two co-located units on the same machine name (gateway-only @ ~/.ava_gateway + agent-runner @
    ~/.ava) compose into ONE machines row: role = union; gateway_url = ops URL
    (agent-runner unit's url, because the host serves agent-runner)."""
    # gateway-only unit
    _machine_setup(name="test-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(_db(database_gate=database_gate), url="http://test-host:8000")
    # agent-runner-only unit (same machine name, different home)
    _machine_setup(name="test-host", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")

    gateway_url, role, _desc, stopped_at = _read_machine("test-host")
    assert role == ["agent-runner", "gateway"]  # sorted union
    assert gateway_url == "http://localhost:8600"  # ops URL of the agent-runner unit
    assert stopped_at is None
    # exactly one machines row, two machine_units rows
    assert machines.list_all(_db(database_gate=database_gate)) == [
        ("test-host", "http://localhost:8600")
    ]


def test_colocated_stop_one_unit_retracts_only_its_caps(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """Stopping the agent-runner unit on a co-located host: composed role downgrades to gateway only,
    gateway_url switches back to the gateway unit's url; host still live (gateway unit running)."""
    _machine_setup(name="test-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(_db(database_gate=database_gate), url="http://test-host:8000")
    _machine_setup(name="test-host", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")

    machines.mark_stopping(
        _db(database_gate=database_gate), "test-host", "~/.ava"
    )  # stop the agent-runner unit

    gateway_url, role, _desc, stopped_at = _read_machine("test-host")
    assert role == ["gateway"]  # agent-runner cap retracted
    assert gateway_url == "http://test-host:8000"  # now the gateway unit's url
    assert stopped_at is None  # gateway unit still live
    # the agent-runner unit dropped out of the fan-out target list
    assert machines.list_agent_runners(_db(database_gate=database_gate)) == []


def test_colocated_restart_unit_recomposes_union(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """Stopped unit re-register_self comes back: composed role again includes agent-runner."""
    _machine_setup(name="test-host", role="gateway", home="~/.ava_gateway")
    machines.register_self(_db(database_gate=database_gate), url="http://test-host:8000")
    _machine_setup(name="test-host", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")
    machines.mark_stopping(_db(database_gate=database_gate), "test-host", "~/.ava")

    _machine_setup(name="test-host", role="agent-runner", home="~/.ava")
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")

    _gateway_url, role, _desc, _stopped = _read_machine("test-host")
    assert role == ["agent-runner", "gateway"]


# register_self loopback dial-URL guard
def test_register_self_rejects_loopback_ops_url_when_gateway_remote(
    _machine_setup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """An agent-runner-only unit whose gateway is REMOTE must not register a
    loopback ops URL — the gateway would dial itself (the 2026-07-18 incident).
    Raised before any DB write."""
    _machine_setup(name="wsl", role="agent-runner")
    monkeypatch.setattr(
        settings.gateway, "gateway_url", "https://gw.example.com:8000"
    )  # remote gateway
    with pytest.raises(machines.LoopbackDialUrlRefused):
        machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")
    # nothing landed in the table
    assert machines.list_all(_db(database_gate=database_gate)) == []


def test_register_self_rejects_loopback_station_url_when_gateway_remote(
    _machine_setup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """An observability-station-only unit whose gateway is REMOTE must not
    register a loopback dial URL — the gateway would probe itself instead of
    the station (WP4 tightening of the runner rule, same 2026-07-18 shape).
    Raised before any DB write."""
    _machine_setup(name="station-wsl", role="observability-station")
    monkeypatch.setattr(
        settings.gateway, "gateway_url", "https://gw.example.com:8000"
    )  # remote gateway
    with pytest.raises(machines.LoopbackDialUrlRefused):
        machines.register_self(_db(database_gate=database_gate), url="http://localhost:4318")
    # nothing landed in the table
    assert machines.list_all(_db(database_gate=database_gate)) == []


def test_register_self_allows_loopback_station_url_when_gateway_colocated(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """A station-only unit whose gateway is co-located (loopback gateway URL)
    may register a loopback dial URL — the zero-config single-box posture."""
    _machine_setup(name="station-local", role="observability-station")
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:4318")
    gateway_url, role, _desc, _stopped = _read_machine("station-local")
    assert gateway_url == "http://localhost:4318"
    assert role == ["observability-station"]


def test_register_self_allows_loopback_ops_url_when_gateway_colocated(
    _machine_setup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """Same loopback ops URL is LEGAL when the gateway is co-located (loopback
    gateway URL) — a split-home single box dials its runner over loopback."""
    _machine_setup(name="box", role="agent-runner")
    monkeypatch.setattr(
        settings.gateway, "gateway_url", "http://localhost:8000"
    )  # co-located gateway
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")
    assert machines.list_all(_db(database_gate=database_gate)) == [("box", "http://localhost:8600")]


def test_register_self_allows_loopback_ops_url_when_also_gateway(
    _machine_setup, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    """A single co-located unit that serves BOTH gateway and agent-runner may
    register a loopback ops URL regardless of its gateway URL — its own gateway
    self-dials over loopback."""
    _machine_setup(name="single", role="gateway,agent-runner")
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://gw.example.com:8000")
    machines.register_self(_db(database_gate=database_gate), url="http://localhost:8600")
    assert machines.list_all(_db(database_gate=database_gate)) == [
        ("single", "http://localhost:8600")
    ]


# register_self description column
def _read_description(name: str) -> str | None:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT description FROM machines WHERE name = %s", (name,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_register_self_writes_description(_machine_setup, database_gate: ProcessDbGate) -> None:
    """register_self picks up machine_description() and UPSERTs it."""
    from base.cluster.machine import set_identity

    _machine_setup(name="desc-machine")
    set_identity(description="voice IO + browser")
    machines.register_self(_db(database_gate=database_gate), url="http://d:8000")
    assert _read_description("desc-machine") == "voice IO + browser"


def test_register_self_description_none_stores_null(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """No description configured → column NULL."""
    from base.cluster.machine import set_identity

    _machine_setup(name="nodesc-machine")
    set_identity(description=None)
    machines.register_self(_db(database_gate=database_gate), url="http://n:8000")
    assert _read_description("nodesc-machine") is None


def test_register_self_updates_description_on_conflict(
    _machine_setup, database_gate: ProcessDbGate
) -> None:
    """Second register_self overwrites description (ON CONFLICT DO UPDATE)."""
    from base.cluster.machine import set_identity

    _machine_setup(name="upd-machine")
    set_identity(description="old")
    machines.register_self(_db(database_gate=database_gate), url="http://u:8000")
    set_identity(description="new")
    machines.register_self(_db(database_gate=database_gate), url="http://u:8000")
    assert _read_description("upd-machine") == "new"


# up_since_at — the announce stamp (#981)
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
