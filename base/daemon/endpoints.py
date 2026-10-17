"""`ServiceEndpoints`: where each health daemon of a unit listens and records its pid.

A daemon's endpoint is a fact of the unit: the healthz port (the fixed port table, or the unit's
`MEIVA_<NAME>_HEALTH_PORT` override) and the pidfile under `$MEIVA_HOME/run`. A composition root
builds the table from its config owner and runtime directory (`ServiceEndpoints.from_boot`)
and hands a daemon its own row
(`endpoints.of("labeler")`) and a supervisor, probe or command the table; nothing below the root
reads the settings or `MEIVA_HOME` for a port or pidfile; the `ambient-endpoint` rule bans building
the table outside the roots a package names.

Legacy roots still use `from_settings` until their config inputs are explicit. Both factories
use the same registered port resolver; a table remains the values at its construction time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from base.config import ConfigBoot, get_field
from base.host.env.health_ports import health_port
from base.host.env.registry import health_port_env_aliases
from base.paths import meiva_home, run_dir


@dataclass(frozen=True)
class ServiceEndpoint:
    """One daemon's healthz port and pidfile."""

    name: str
    health_port: int
    pidfile: Path

    def healthz_url(self, host: str = "127.0.0.1") -> str:
        """The `/healthz` URL of this daemon as reached from `host`."""
        return f"http://{host}:{self.health_port}/healthz"


@dataclass(frozen=True)
class ServiceEndpoints:
    """The endpoint of every health daemon, by service name."""

    by_name: Mapping[str, ServiceEndpoint]

    @classmethod
    def from_settings(cls) -> ServiceEndpoints:
        """The table as the live settings and `MEIVA_HOME` describe it now."""
        return cls._from_fields(get_field, runtime=run_dir(meiva_home()))

    @classmethod
    def from_boot(cls, config: ConfigBoot, *, runtime: Path) -> ServiceEndpoints:
        """Capture the caller's current port fields and explicit runtime directory."""
        return cls._from_fields(config.get_field, runtime=runtime)

    @classmethod
    def _from_fields(
        cls, read_field: Callable[[str], int | None], *, runtime: Path
    ) -> ServiceEndpoints:
        return cls(
            {
                name: ServiceEndpoint(
                    name, health_port(name, read_field=read_field), runtime / f"{name}.pid"
                )
                for name in health_port_env_aliases()
            }
        )

    def of(self, name: str) -> ServiceEndpoint:
        """The endpoint of `name`; an unregistered daemon is a KeyError (new daemons register
        a port first, as `health_port` already requires)."""
        return self.by_name[name]

    def __iter__(self) -> Iterator[ServiceEndpoint]:
        return iter(self.by_name.values())
