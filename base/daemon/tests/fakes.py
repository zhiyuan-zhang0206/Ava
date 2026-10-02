"""Pin the endpoint table for tests whose code under test builds it with
`ServiceEndpoints.from_settings()` and needs a specific port or pidfile directory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints


def pin_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    *,
    port: Callable[[str], int] | None = None,
    pid_dir: Path | None = None,
) -> None:
    """`from_settings()` returns the live table with each health port replaced by `port(name)`
    and each pidfile moved to `pid_dir/<name>.pid`, where given."""
    live = ServiceEndpoints.from_settings()
    table = ServiceEndpoints(
        {
            name: ServiceEndpoint(
                name,
                endpoint.health_port if port is None else port(name),
                endpoint.pidfile if pid_dir is None else pid_dir / f"{name}.pid",
            )
            for name, endpoint in live.by_name.items()
        }
    )

    def from_settings(_cls: type[ServiceEndpoints]) -> ServiceEndpoints:
        return table

    monkeypatch.setattr(ServiceEndpoints, "from_settings", classmethod(from_settings))
