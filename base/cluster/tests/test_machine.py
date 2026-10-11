"""`base.cluster.machine` contracts — explicitly owned resolution and validation.

MachineIdentity.role is derived from independent capability booleans
(serve_gateway / serve_agent_runner / serve_observability_station), each the settings
bool of its `AVA_MACHINE_SERVE_*` key (None = off). The home's `.env` is the only source:
identity files in the home are not read (`test_identity_files_in_the_home_are_not_read`).
Role injection is exercised through the public MachineIdentity.set/role contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster import machine as _machine
from base.cluster.machine import (
    MachineIdentity,
    MachineNameMissing,
    MachineRoleInvalid,
    MachineRoleMissing,
    format_capabilities,
    gateway_auth_headers,
    roles_from_flags,
)
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot, settings


def test_owned_identity_reads_fields_independently_and_caches_absence() -> None:
    reads: list[str] = []

    def name() -> str:
        reads.append("name")
        raise MachineNameMissing("not configured")

    def role() -> frozenset[str]:
        reads.append("role")
        return roles_from_flags(
            serve_gateway=False,
            serve_agent_runner=True,
            serve_observability_station=False,
        )

    def host() -> str:
        reads.append("host")
        return "  "

    def description() -> None:
        reads.append("description")

    owner = MachineIdentity(name=name, role=role, host=host, description=description)
    assert reads == []
    assert owner.role() == owner.role() == frozenset({"agent-runner"})
    assert owner.description() is owner.description() is None
    assert owner.host() == owner.host() == "localhost"
    assert reads == ["role", "description", "host"]
    with pytest.raises(MachineNameMissing):
        owner.name()
    assert reads[-1] == "name"


def test_owned_identity_reset_reloads_only_its_own_readers() -> None:
    names = [" initial-a ", "initial-b"]
    first = MachineIdentity(
        name=lambda: names[0],
        role=lambda: frozenset({"gateway"}),
        host=lambda: " host-a ",
        description=lambda: " details ",
    )
    second = MachineIdentity(
        name=lambda: names[1],
        role=lambda: frozenset({"agent-runner"}),
        host=lambda: "host-b",
        description=lambda: None,
    )
    assert first.name() == "initial-a"
    assert second.name() == "initial-b"
    assert first.host() == "host-a"
    assert first.description() == "details"
    names[:] = ["changed-a", "changed-b"]
    first.set(role="observability-station", description=None)
    assert first.role() == frozenset({"observability-station"})
    assert first.description() is None
    assert first.name() == "initial-a"
    first.reset()
    assert first.name() == "changed-a"
    assert first.role() == frozenset({"gateway"})
    assert first.description() == "details"
    assert second.name() == "initial-b"
    assert second.role() == frozenset({"agent-runner"})


def test_owned_identity_rejects_invalid_supplied_facts() -> None:
    owner = MachineIdentity(
        name=lambda: " ",
        role=lambda: frozenset({"typo"}),
        host=lambda: "",
        description=lambda: None,
    )
    with pytest.raises(MachineNameMissing):
        owner.name()
    with pytest.raises(MachineRoleInvalid):
        owner.role()
    with pytest.raises(MachineRoleMissing):
        roles_from_flags(
            serve_gateway=False,
            serve_agent_runner=False,
            serve_observability_station=False,
        )


@pytest.fixture
def machine_config() -> ConfigBoot:
    """A test root owns its snapshot and field repairs, independent of other roots."""
    config = ConfigBoot()
    config.read_process_environment()
    for field in (
        "machine_serve_gateway",
        "machine_serve_agent_runner",
        "machine_serve_observability_station",
    ):
        config.set_field(field, None)
    return config


@pytest.fixture
def machine_identity(machine_config: ConfigBoot) -> MachineIdentity:
    return machine_from_boot(machine_config)


def test_machine_role_follows_each_settings_flag(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:
    machine_config.set_field("machine_serve_gateway", True)
    machine_config.set_field("machine_serve_agent_runner", False)
    assert machine_identity.role() == frozenset({"gateway"})


def test_machine_role_single_box(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:
    """Both capability flags true -> single-box gateway,agent-runner set."""
    machine_config.set_field("machine_serve_gateway", True)
    machine_config.set_field("machine_serve_agent_runner", True)
    assert machine_identity.role() == frozenset({"gateway", "agent-runner"})


def test_machine_role_observability_station(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:
    """The observability-station capability resolves from its own flag —
    a second machine can declare the station without gateway or agent-runner."""
    machine_config.set_field("machine_serve_observability_station", True)
    assert machine_identity.role() == frozenset({"observability-station"})


def test_is_observability_station_helper(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:
    """is_observability_station() mirrors is_gateway()/is_agent_runner()."""

    machine_config.set_field("machine_serve_observability_station", True)
    assert ("observability-station" in machine_identity.role()) is True
    assert ("gateway" in machine_identity.role()) is False
    # machine_identity.role() is cached by this owner — the identity holder must be reset for
    # the re-resolve after the capability flags change.
    machine_identity.reset()
    machine_config.set_field("machine_serve_observability_station", None)
    machine_config.set_field("machine_serve_gateway", True)
    assert ("observability-station" in machine_identity.role()) is False
    assert ("gateway" in machine_identity.role()) is True


def test_machine_role_raises_when_neither(machine_identity: MachineIdentity) -> None:
    # every flag unset -> no capability -> Missing.
    with pytest.raises(MachineRoleMissing):
        machine_identity.role()


def test_machine_role_raises_when_both_false(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:
    """Explicit false in both flags -> no capability -> Missing (not a silent
    empty set)."""
    machine_config.set_field("machine_serve_gateway", False)
    machine_config.set_field("machine_serve_agent_runner", False)
    with pytest.raises(MachineRoleMissing):
        machine_identity.role()


def test_identity_files_in_the_home_are_not_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    machine_config: ConfigBoot,
    machine_identity: MachineIdentity,
) -> None:
    """The home's `.env` is the only identity source: the `machine_*`, `gateway_url`
    and `memory_remote` files older homes kept are not read by any resolver."""
    from base.cluster.machine import GatewayApiBaseMissing, MachineNameMissing

    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    machine_config.set_field("machine_name", "")
    machine_config.set_field("machine_host", "")
    machine_config.set_field("machine_description", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    (tmp_path / "machine_name").write_text("from-file")
    (tmp_path / "machine_host").write_text("10.0.0.2")
    (tmp_path / "machine_description").write_text("from-file")
    (tmp_path / "machine_serve_gateway").write_text("true")
    (tmp_path / "machine_serve_agent_runner").write_text("true")
    (tmp_path / "machine_serve_observability_station").write_text("true")
    (tmp_path / "gateway_url").write_text("http://from-file:8000")

    with pytest.raises(MachineNameMissing):
        machine_identity.name()
    with pytest.raises(MachineRoleMissing):
        machine_identity.role()
    with pytest.raises(GatewayApiBaseMissing):
        _machine.gateway_api_base()
    assert machine_identity.description() is None
    assert machine_identity.host() == "localhost"


def test_owned_identity_set_coerces_string_and_iterable(machine_identity: MachineIdentity) -> None:
    """MachineIdentity.set accepts a comma string or an iterable; both validate to a set."""

    machine_identity.set(role="gateway,agent-runner")
    try:
        assert machine_identity.role() == frozenset({"gateway", "agent-runner"})
    finally:
        machine_identity.reset()
    machine_identity.set(role=["gateway", "agent-runner"])
    try:
        assert machine_identity.role() == frozenset({"gateway", "agent-runner"})
    finally:
        machine_identity.reset()


# Public identity injection validates comma strings and iterable capability sets,
# guarding invalid-token and empty-set rejections without accessing its private parser.


def test_parse_roles_comma_set(machine_identity: MachineIdentity) -> None:
    """A comma capability set parses to a 2-element frozenset (single box)."""
    machine_identity.set(role="gateway,agent-runner")
    assert machine_identity.role() == frozenset({"gateway", "agent-runner"})


def test_parse_roles_tolerates_whitespace(machine_identity: MachineIdentity) -> None:
    machine_identity.set(role=" gateway , agent-runner ")
    assert machine_identity.role() == frozenset({"gateway", "agent-runner"})


def test_parse_roles_empty_after_split_raises(machine_identity: MachineIdentity) -> None:
    """An all-comma / blank value yields no valid token -> MachineRoleInvalid,
    not a silent empty 'no capability' set."""
    with pytest.raises(MachineRoleInvalid):
        machine_identity.set(role=",,")


def test_parse_roles_one_bad_token_rejects_whole_set(machine_identity: MachineIdentity) -> None:
    with pytest.raises(MachineRoleInvalid):
        machine_identity.set(role="gateway,bogus")


def test_parse_roles_accepts_observability_station(machine_identity: MachineIdentity) -> None:
    """The station token parses alongside the classic capabilities."""
    machine_identity.set(role="gateway,observability-station")
    assert machine_identity.role() == frozenset({"gateway", "observability-station"})
    machine_identity.set(role="observability-station")
    assert machine_identity.role() == frozenset({"observability-station"})


def test_machine_description_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:

    machine_config.set_field("machine_description", " voice IO + browser \n")
    assert machine_identity.description() == "voice IO + browser"


def test_machine_description_none_when_empty(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:

    machine_config.set_field("machine_description", "")
    assert machine_identity.description() is None


def test_gateway_api_base_resolves_configured_url_role_blind(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot
) -> None:
    # Rung 0.A / §2 rule 4: a gateway-capable host resolves the SAME configured
    # gateway_url as any other caller — no localhost shortcut, role is not read.
    machine_config.set_field("machine_serve_gateway", True)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://gw.vpn:8000/")
    assert _machine.gateway_api_base() == "http://gw.vpn:8000"


def test_gateway_api_base_agent_runner_uses_gateway_url(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot
) -> None:
    machine_config.set_field("machine_serve_agent_runner", True)
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://ava.example.com")
    assert _machine.gateway_api_base() == "https://ava.example.com"


def test_gateway_api_base_unset_raises_regardless_of_role(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot
) -> None:
    # unset raises GatewayApiBaseMissing for every capability set —
    # gateway_api_base() is role-blind, so the serve flags do not change it.
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    for serve_gateway, serve_agent_runner in ((True, False), (False, True), (True, True)):
        machine_config.set_field("machine_serve_gateway", serve_gateway)
        machine_config.set_field("machine_serve_agent_runner", serve_agent_runner)
        with pytest.raises(_machine.GatewayApiBaseMissing):
            _machine.gateway_api_base()


def test_owned_identity_set_overrides_resolution(
    tmp_path: Path, machine_identity: MachineIdentity
) -> None:

    machine_identity.set(name="injected", role="gateway", description="desc")
    try:
        assert machine_identity.name() == "injected"
        assert machine_identity.role() == frozenset({"gateway"})
        assert machine_identity.description() == "desc"
    finally:
        machine_identity.reset()


def test_owned_identity_set_is_per_field_independent(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:
    """Injecting only role must not force name resolution (a role-only test
    should never hit MachineNameMissing)."""

    machine_config.set_field("machine_name", "")
    machine_identity.set(role="agent-runner")
    try:
        assert machine_identity.role() == frozenset(
            {"agent-runner"}
        )  # name never resolved -> no raise
    finally:
        machine_identity.reset()


def test_owned_identity_reset_reresolves(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:

    machine_identity.set(role="gateway")
    assert machine_identity.role() == frozenset({"gateway"})
    machine_identity.reset()
    machine_config.set_field("machine_serve_agent_runner", True)
    assert machine_identity.role() == frozenset({"agent-runner"})
    machine_identity.reset()


def test_owned_identity_set_description_none_is_explicit(
    tmp_path: Path, machine_identity: MachineIdentity
) -> None:
    """description=None injects None (resolved), not 'leave unset'."""

    machine_identity.set(description=None)
    try:
        assert machine_identity.description() is None
    finally:
        machine_identity.reset()


def test_reachable_host_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:
    machine_config.set_field("machine_host", " 10.0.0.1 ")
    assert machine_identity.host() == "10.0.0.1"


def test_reachable_host_defaults_to_localhost(
    monkeypatch: pytest.MonkeyPatch, machine_config: ConfigBoot, machine_identity: MachineIdentity
) -> None:
    """Empty -> `localhost` (single-box zero-config). A remote runner that lands
    here is rejected at registration time, not by this resolver."""
    machine_config.set_field("machine_host", "")
    assert machine_identity.host() == "localhost"


def test_owned_identity_set_host_injection(machine_identity: MachineIdentity) -> None:

    machine_identity.set(host="10.0.0.5")
    try:
        assert machine_identity.host() == "10.0.0.5"
    finally:
        machine_identity.reset()


def test_format_capabilities_labels() -> None:
    """Capability booleans rendered as a single label — shared by ava status / cluster status."""
    assert format_capabilities(True, True) == "gateway + agent-runner"
    assert format_capabilities(True, False) == "gateway"
    assert format_capabilities(False, True) == "agent-runner"
    assert format_capabilities(False, False) == "none"


def test_format_capabilities_station_labels() -> None:
    """The observability-station flag joins the label in every combination — a
    pure station host renders as "observability-station", and mixed hosts list
    it in capability order (gateway, agent-runner, observability-station)."""
    assert format_capabilities(False, False, True) == "observability-station"
    assert format_capabilities(True, False, True) == "gateway + observability-station"
    assert format_capabilities(False, True, True) == "agent-runner + observability-station"
    assert format_capabilities(True, True, True) == "gateway + agent-runner + observability-station"
    # The third flag defaults False — a caller that only knows the two original
    # flags keeps the pre-station label.
    assert format_capabilities(True, True) == "gateway + agent-runner"
    assert format_capabilities(False, False) == "none"


def test_gateway_auth_headers_present_when_secret_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When secret is set, includes Bearer — every /api/cluster/* client call relies on it for gateway auth."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cr3t")
    assert gateway_auth_headers() == {"Authorization": "Bearer s3cr3t"}


def test_gateway_auth_headers_empty_when_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """secret empty (dev/test, middleware no-op) returns empty dict; both modes usable at the same call site."""
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    assert gateway_auth_headers() == {}
