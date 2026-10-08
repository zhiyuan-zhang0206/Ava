"""Which credential a process presents to the gateway API (`gateway_bearer`).

An authenticated cluster delivers every agent host and runner service its write
generation's machine API token. A process holding that token presents it; an
operator or gateway-profile process without one presents the human secret it
legitimately holds; an agent- or runner-profile process without one raises
instead of silently presenting the human secret. A remote-managed data plane
keeps no write generations and delivers no token, so its gateway home keeps
presenting the human secret.
"""

from __future__ import annotations

import pytest

from base.cluster import machine
from base.cluster.auth import API_TOKEN_ENV, bearer_header
from base.cluster.machine import gateway_auth_headers
from base.config import settings

SECRET = "human-" + "h" * 40
TOKEN = "delivered-" + "t" * 32
LAUNCHER_PROFILE_ENV = "AVA_LAUNCHER_PROFILE"


def _process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str | None,
    token: str | None,
    secret: str,
    recorded_profile: str | None = None,
) -> None:
    """Make this process look like one launched under `profile` (live marker) or
    descended from `recorded_profile` (a CLI that popped the marker), holding
    `token` and a settings `secret`."""
    for name, value in (
        ("AVA_PROCESS_PROFILE", profile),
        (LAUNCHER_PROFILE_ENV, recorded_profile),
        (API_TOKEN_ENV, token),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)


def _remote_managed_gateway_home(monkeypatch: pytest.MonkeyPatch, *, serves_gateway: bool) -> None:
    monkeypatch.setattr(type(settings.data_plane), "is_remote", property(lambda _self: True))
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: serves_gateway)


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_a_delivered_token_is_presented_before_the_secret(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    _process(monkeypatch, profile=profile, token=TOKEN, secret=SECRET)
    assert gateway_auth_headers() == bearer_header(TOKEN)


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_an_open_cluster_presents_nothing(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    """No token and no secret (the single-box default): no header, whatever the profile."""
    _process(monkeypatch, profile=profile, token=None, secret="")
    assert gateway_auth_headers() == {}


@pytest.mark.parametrize("profile", [None, "gateway"])
def test_operator_and_gateway_processes_without_a_token_present_the_secret(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    _process(monkeypatch, profile=profile, token=None, secret=SECRET)
    assert gateway_auth_headers() == bearer_header(SECRET)


@pytest.mark.parametrize("profile", ["agent", "runner"])
def test_agent_and_runner_processes_without_a_token_fail_instead_of_using_the_secret(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    _process(monkeypatch, profile=profile, token=None, secret=SECRET)

    with pytest.raises(RuntimeError, match="AVA_API_TOKEN") as raised:
        gateway_auth_headers()

    message = str(raised.value)
    assert f"{profile}-profile" in message
    assert SECRET not in message


def test_the_failure_is_the_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _process(monkeypatch, profile="agent", token=None, secret=SECRET)
    with pytest.raises(machine.GatewayApiTokenMissing):
        machine.gateway_bearer()


def test_a_cli_started_inside_an_agent_is_held_to_the_same_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI entry pops the live marker and records it as AVA_LAUNCHER_PROFILE."""
    _process(monkeypatch, profile=None, token=None, secret=SECRET, recorded_profile="agent")
    with pytest.raises(RuntimeError, match="AVA_API_TOKEN"):
        gateway_auth_headers()


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_a_remote_managed_gateway_home_keeps_presenting_the_secret(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    """The plane keeps no write generations, so the launcher delivers no token
    (`api_delivery`) and its gateway-local services present the human secret."""
    _process(monkeypatch, profile=profile, token=None, secret=SECRET)
    _remote_managed_gateway_home(monkeypatch, serves_gateway=True)
    assert gateway_auth_headers() == bearer_header(SECRET)


def test_a_unit_that_does_not_serve_the_gateway_is_never_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure agent-runner takes its token from its installed capability; a
    foreign data-plane host there is the gateway's, not a remote-managed plane."""
    _process(monkeypatch, profile="runner", token=None, secret=SECRET)
    _remote_managed_gateway_home(monkeypatch, serves_gateway=False)
    with pytest.raises(RuntimeError, match="AVA_API_TOKEN"):
        gateway_auth_headers()
