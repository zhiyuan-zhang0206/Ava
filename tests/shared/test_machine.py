"""`shared/machine.py` unit tests — machine_name and machine_role env/file precedence + validation.

machine_role is derived from two independent capability booleans (serve_gateway / serve_agent_runner);
each boolean: env (settings bool|None) > `$AVA_HOME/machine_serve_*` file > False.
`_parse_roles` / `_coerce_roles` are kept, only used in the set_identity injection path (see bottom).
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from shared import machine as _machine
from shared.config import settings
from shared.machine import (
    MachineRoleInvalid,
    MachineRoleMissing,
    format_capabilities,
    gateway_auth_headers,
    machine_role,
)


@pytest.fixture(autouse=True)
def _machine_setup(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Reset identity holder + clear both serve-capability env settings so a
    leaked host value never bleeds into a precedence test. Each test sets only
    what it needs; unset (None) means "fall through to the file"."""
    from shared.machine import reset_identity

    monkeypatch.setattr(settings.general, "machine_serve_gateway", None)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", None)
    monkeypatch.setattr(settings.general, "machine_serve_observability_station", None)
    reset_identity()
    yield
    reset_identity()


def test_machine_role_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # env (settings bool) beats the on-disk file for each capability.
    monkeypatch.setattr(settings.general, "machine_serve_gateway", True)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", False)
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_serve_gateway").write_text("false")  # overridden by env
    (tmp_path / "machine_serve_agent_runner").write_text("true")  # overridden by env
    assert machine_role() == frozenset({"gateway"})


def test_machine_role_file_when_env_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # env unset (None) -> each capability reads its file.
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_serve_agent_runner").write_text("true\n")  # trailing \n trimmed
    assert machine_role() == frozenset({"agent-runner"})


def test_machine_role_both_files_single_box(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both capability files true -> single-box gateway,agent-runner set."""
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_serve_gateway").write_text("true")
    (tmp_path / "machine_serve_agent_runner").write_text("true")
    assert machine_role() == frozenset({"gateway", "agent-runner"})


def test_machine_role_observability_station_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The observability-station capability resolves from its own flag file —
    a second machine can declare the station without gateway or agent-runner."""
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_serve_observability_station").write_text("true\n")
    assert machine_role() == frozenset({"observability-station"})


def test_is_observability_station_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """is_observability_station() mirrors is_gateway()/is_agent_runner()."""
    from shared.machine import reset_identity

    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_serve_observability_station").write_text("true")
    assert _machine.is_observability_station() is True
    assert _machine.is_gateway() is False
    # machine_role() is process-cached — the identity holder must be reset for
    # the re-resolve after the capability files change.
    reset_identity()
    (tmp_path / "machine_serve_observability_station").unlink()
    (tmp_path / "machine_serve_gateway").write_text("true")
    assert _machine.is_observability_station() is False
    assert _machine.is_gateway() is True


def test_machine_role_raises_when_neither(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # env unset + no files -> both capabilities resolve False -> Missing.
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    with pytest.raises(MachineRoleMissing):
        machine_role()


def test_machine_role_raises_when_both_files_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit false in both files -> no capability -> Missing (not a silent
    empty set)."""
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_serve_gateway").write_text("false")
    (tmp_path / "machine_serve_agent_runner").write_text("false")
    with pytest.raises(MachineRoleMissing):
        machine_role()


def test_machine_serve_file_typo_raises_not_silent_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typo in a machine_serve_* file (e.g. 'ture') must raise, not silently
    resolve to False — else a host meant to serve gateway quietly serves nothing."""
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_serve_gateway").write_text("ture")  # typo for 'true'
    with pytest.raises(MachineRoleInvalid):
        machine_role()


def test_set_identity_coerces_string_and_iterable() -> None:
    """set_identity accepts a comma string or an iterable; both validate to a set."""
    from shared.machine import reset_identity, set_identity

    set_identity(role="gateway,agent-runner")
    try:
        assert machine_role() == frozenset({"gateway", "agent-runner"})
    finally:
        reset_identity()
    set_identity(role=["gateway", "agent-runner"])
    try:
        assert machine_role() == frozenset({"gateway", "agent-runner"})
    finally:
        reset_identity()


# `_parse_roles` validates the comma-string set_identity() / the DB TEXT[] feed
# into the frozenset. Driven directly here (no longer reachable via env/file,
# which now use two booleans), guarding the invalid-token / empty rejections.


def test_parse_roles_comma_set() -> None:
    """A comma capability set parses to a 2-element frozenset (single box)."""
    assert _machine._parse_roles("gateway,agent-runner") == frozenset({"gateway", "agent-runner"})


def test_parse_roles_tolerates_whitespace() -> None:
    assert _machine._parse_roles(" gateway , agent-runner ") == frozenset(
        {"gateway", "agent-runner"}
    )


def test_parse_roles_empty_after_split_raises() -> None:
    """An all-comma / blank value yields no valid token -> MachineRoleInvalid,
    not a silent empty 'no capability' set."""
    with pytest.raises(MachineRoleInvalid):
        _machine._parse_roles(",,")


def test_parse_roles_one_bad_token_rejects_whole_set() -> None:
    with pytest.raises(MachineRoleInvalid):
        _machine._parse_roles("gateway,bogus")


def test_parse_roles_accepts_observability_station() -> None:
    """The station token parses alongside the classic capabilities."""
    assert _machine._parse_roles("gateway,observability-station") == frozenset(
        {"gateway", "observability-station"}
    )
    assert _machine._parse_roles("observability-station") == frozenset({"observability-station"})


def test_machine_description_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from shared import machine
    from shared.config import settings

    monkeypatch.setattr(settings.general, "machine_description", "from-env")
    monkeypatch.setattr(machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_description").write_text("from-file")
    assert machine.machine_description() == "from-env"


def test_machine_description_file_when_env_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from shared import machine
    from shared.config import settings

    monkeypatch.setattr(settings.general, "machine_description", "")
    monkeypatch.setattr(machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_description").write_text("voice IO + browser\n")
    assert machine.machine_description() == "voice IO + browser"


def test_machine_description_none_when_neither(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from shared import machine
    from shared.config import settings

    monkeypatch.setattr(settings.general, "machine_description", "")
    monkeypatch.setattr(machine, "ava_home", lambda: tmp_path)
    assert machine.machine_description() is None


def test_gateway_api_base_resolves_configured_url_role_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rung 0.A / §2 rule 4: a gateway-capable host resolves the SAME configured
    # gateway_url as any other caller — no localhost shortcut, role is not read.
    monkeypatch.setattr(settings.general, "machine_serve_gateway", True)
    monkeypatch.setattr(settings.gateway, "gateway_url", "http://gw.vpn:8000/")
    assert _machine.gateway_api_base() == "http://gw.vpn:8000"


def test_gateway_api_base_agent_runner_uses_gateway_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", True)
    monkeypatch.setattr(settings.gateway, "gateway_url", "https://ava.example.com")
    assert _machine.gateway_api_base() == "https://ava.example.com"


def test_gateway_api_base_reads_file_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # env unset -> falls back to $AVA_HOME/gateway_url (unified with
    # machines.gateway_url(), so a host with only the file set still resolves).
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "gateway_url").write_text("http://from-file:8000\n")
    assert _machine.gateway_api_base() == "http://from-file:8000"


def test_gateway_api_base_unset_raises_regardless_of_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # unset (env empty + no file) raises GatewayApiBaseMissing for every capability
    # set — gateway_api_base() is role-blind, so the serve flags do not change it.
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    for serve_gateway, serve_agent_runner in ((True, False), (False, True), (True, True)):
        monkeypatch.setattr(settings.general, "machine_serve_gateway", serve_gateway)
        monkeypatch.setattr(settings.general, "machine_serve_agent_runner", serve_agent_runner)
        with pytest.raises(_machine.GatewayApiBaseMissing):
            _machine.gateway_api_base()


def test_set_identity_overrides_resolution(tmp_path: Path) -> None:
    from shared.machine import (
        machine_description,
        machine_name,
        machine_role,
        reset_identity,
        set_identity,
    )

    set_identity(name="injected", role="gateway", description="desc")
    try:
        assert machine_name() == "injected"
        assert machine_role() == frozenset({"gateway"})
        assert machine_description() == "desc"
    finally:
        reset_identity()


def test_set_identity_is_per_field_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injecting only role must not force name resolution (a role-only test
    should never hit MachineNameMissing)."""
    from shared.machine import machine_role, reset_identity, set_identity

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(_machine, "ava_home", lambda: Path("/nonexistent-ava-home"))
    set_identity(role="agent-runner")
    try:
        assert machine_role() == frozenset({"agent-runner"})  # name never resolved -> no raise
    finally:
        reset_identity()


def test_reset_identity_reresolves(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.machine import machine_role, reset_identity, set_identity

    set_identity(role="gateway")
    assert machine_role() == frozenset({"gateway"})
    reset_identity()
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", True)
    assert machine_role() == frozenset({"agent-runner"})
    reset_identity()


def test_set_identity_description_none_is_explicit(tmp_path: Path) -> None:
    """description=None injects None (resolved), not 'leave unset'."""
    from shared.machine import machine_description, reset_identity, set_identity

    set_identity(description=None)
    try:
        assert machine_description() is None
    finally:
        reset_identity()


def test_reachable_host_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings.general, "machine_host", "100.64.0.1")
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_host").write_text("100.0.0.9")  # overridden by env
    assert _machine.reachable_host() == "100.64.0.1"


def test_reachable_host_file_when_env_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings.general, "machine_host", "")
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    (tmp_path / "machine_host").write_text("100.64.0.2\n")  # trailing \n trimmed
    assert _machine.reachable_host() == "100.64.0.2"


def test_reachable_host_defaults_to_localhost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """env empty + no machine_host file → `localhost` (single-box zero-config). The
    fallback is last so the enrolled runner's file wins; a remote runner that lands
    here is rejected at registration time, not by this resolver."""
    monkeypatch.setattr(settings.general, "machine_host", "")
    monkeypatch.setattr(_machine, "ava_home", lambda: tmp_path)
    assert _machine.reachable_host() == "localhost"


def test_set_identity_host_injection() -> None:
    from shared.machine import reachable_host, reset_identity, set_identity

    set_identity(host="100.64.0.5")
    try:
        assert reachable_host() == "100.64.0.5"
    finally:
        reset_identity()


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
