"""Registration test roots retain one machine owner and an explicit unit home."""

from base.cluster.machine import MachineIdentity
from base.cluster.machines import register_self
from base.config import settings
from base.db import Database


class MachineSetup:
    def __init__(self, machine: MachineIdentity) -> None:
        self.machine = machine
        self.home = "~/.ava"

    def __call__(self, *, name: str, role: str = "gateway", home: str = "~/.ava") -> None:
        self.machine.set(name=name, role=role)
        self.home = home

    def set_home(self, home: str) -> None:
        self.home = home

    def register(self, database: Database, url: str | None = None) -> None:
        register_self(
            database,
            url=url,
            name=self.machine.name(),
            roles=self.machine.role(),
            description=self.machine.description(),
            home=self.home,
            gateway=settings.gateway.gateway_url.strip() or None,
        )
