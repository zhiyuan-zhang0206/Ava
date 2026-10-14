"""Typed stand-ins for the gateway helpers a test installs on a module under test.

`monkeypatch.setattr(module, "gateway_api_base", gateway_api_base_stub(url))`: the stub answers `url`
whatever it is asked, with the signature of the helper it replaces.
"""

from __future__ import annotations

from collections.abc import Callable

from base.cluster.machine import GatewayAccess

__all__ = [
    "daemon_acceptance_stub",
    "gateway_api_base_raises",
    "gateway_api_base_stub",
    "gateway_auth_headers_stub",
]


def gateway_api_base_stub(url: str) -> Callable[[str], str]:
    return lambda _gateway_url: url


def gateway_api_base_raises(error: Exception) -> Callable[[str], str]:
    def refuse(_gateway_url: str) -> str:
        raise error

    return refuse


def gateway_auth_headers_stub(headers: dict[str, str]) -> Callable[[GatewayAccess], dict[str, str]]:
    return lambda _access: headers


def daemon_acceptance_stub(digests: frozenset[str]) -> Callable[[GatewayAccess], frozenset[str]]:
    return lambda _access: digests
