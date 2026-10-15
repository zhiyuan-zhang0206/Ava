from __future__ import annotations

import pytest

from base.cluster import machine
from base.cluster.machine import GatewayAccess, gateway_auth_headers
from base.host.env import bootstrap
from base.host.net.auth import API_TOKEN_ENV, bearer_header

SECRET = "human-" + "h" * 40
TOKEN = "delivered-" + "t" * 32
LAUNCHER_PROFILE_ENV = "AVA_LAUNCHER_PROFILE"


def _access(secret: str, *, remote_plane: bool = False) -> GatewayAccess:
    return GatewayAccess(
        gateway_url="http://gateway.test:8000", cluster_secret=secret, remote_plane=remote_plane
    )


def _process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str | None,
    token: str | None,
    recorded_profile: str | None = None,
) -> None:
    for name, value in (
        ("AVA_PROCESS_PROFILE", profile),
        (LAUNCHER_PROFILE_ENV, recorded_profile),
        (API_TOKEN_ENV, token),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _gateway_home_serving(monkeypatch: pytest.MonkeyPatch, *, serves_gateway: bool) -> None:
    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: serves_gateway)


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_a_delivered_token_is_presented_before_the_secret(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    _process(monkeypatch, profile=profile, token=TOKEN)
    assert gateway_auth_headers(_access(SECRET)) == bearer_header(TOKEN)


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_a_process_with_no_credential_fails_instead_of_presenting_nothing(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    _process(monkeypatch, profile=profile, token=None)
    with pytest.raises(machine.GatewayApiTokenMissing, match="AVA_API_TOKEN"):
        gateway_auth_headers(_access(""))


@pytest.mark.parametrize("profile", [None, "gateway"])
def test_operator_and_gateway_processes_without_a_token_present_the_secret(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    _process(monkeypatch, profile=profile, token=None)
    assert gateway_auth_headers(_access(SECRET)) == bearer_header(SECRET)


@pytest.mark.parametrize("profile", ["agent", "runner"])
def test_agent_and_runner_processes_without_a_token_fail_instead_of_using_the_secret(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    _process(monkeypatch, profile=profile, token=None)

    with pytest.raises(RuntimeError, match="AVA_API_TOKEN") as raised:
        gateway_auth_headers(_access(SECRET))

    message = str(raised.value)
    assert f"{profile}-profile" in message
    assert SECRET not in message


def test_the_failure_is_the_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _process(monkeypatch, profile="agent", token=None)
    with pytest.raises(machine.GatewayApiTokenMissing):
        machine.gateway_bearer(_access(SECRET))


def test_a_cli_started_inside_an_agent_is_held_to_the_same_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _process(monkeypatch, profile=None, token=None, recorded_profile="agent")
    with pytest.raises(RuntimeError, match="AVA_API_TOKEN"):
        gateway_auth_headers(_access(SECRET))


@pytest.mark.parametrize("profile", [None, "gateway", "agent", "runner"])
def test_a_remote_managed_gateway_home_keeps_presenting_the_secret(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    _process(monkeypatch, profile=profile, token=None)
    _gateway_home_serving(monkeypatch, serves_gateway=True)
    assert gateway_auth_headers(_access(SECRET, remote_plane=True)) == bearer_header(SECRET)


def test_a_unit_that_does_not_serve_the_gateway_is_never_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _process(monkeypatch, profile="runner", token=None)
    _gateway_home_serving(monkeypatch, serves_gateway=False)
    with pytest.raises(RuntimeError, match="AVA_API_TOKEN"):
        gateway_auth_headers(_access(SECRET, remote_plane=True))


def test_explicit_readers_preserve_token_first_and_profile_admission_order() -> None:
    reads: list[str] = []

    def forbidden_secret() -> str:
        raise AssertionError("a delivered token must not consult the secret")

    assert (
        machine.resolve_gateway_bearer(
            token_reader=lambda: TOKEN,
            secret_reader=forbidden_secret,
            profile_reader=lambda: pytest.fail("a delivered token must not consult the profile"),
            no_tokens_reader=lambda: pytest.fail("a delivered token must not inspect the plane"),
        )
        == TOKEN
    )

    def token() -> str:
        reads.append("token")
        return ""

    def secret() -> str:
        reads.append("secret")
        return SECRET

    def profile() -> str:
        reads.append("profile")
        return "agent"

    def no_tokens() -> bool:
        reads.append("plane")
        return False

    with pytest.raises(machine.GatewayApiTokenMissing):
        machine.resolve_gateway_bearer(
            token_reader=token,
            secret_reader=secret,
            profile_reader=profile,
            no_tokens_reader=no_tokens,
        )
    assert reads == ["token", "secret", "profile", "plane"]
