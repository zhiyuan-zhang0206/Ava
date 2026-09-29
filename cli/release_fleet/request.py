"""Fleet release requests: one coordinator request per cluster, one unit request per remote unit.

A `FleetRequest` is the gateway home's release request. Its own previous,
candidate and executor images are the gateway unit's; it also carries every
other registered unit, included (`UnitSpec`) or excluded (`Exclusion`), the
captured `FleetPolicy`, and the coordinator endpoint remote unit executors
reach. A single box is a fleet of one: no units, no exclusions, no endpoint.

A `UnitRequest` is derived from the fleet request for one remote unit and
delivered at dispatch. It is that unit's home request, so the home journal,
lock, native adapters and configuration gate apply unchanged. Its id and
`created_at` are the fleet's, so the maintenance hold identity
`(fleet id, created_at)` is the same on every unit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from base.deploy.release.runtime_release import VerifiedRelease
from cli.release_fleet.policy import FleetPolicy, UnitKey
from cli.release_fleet.publication import FleetRelease
from cli.release_transition.request import (
    Digest,
    Record,
    ReleaseRef,
    Request,
    sql_inventory,
)

AdapterKind = Literal["linux-systemd-v1", "darwin-launchd-v1"]
# A remote unit never serves the gateway: the coordinator's home is the one gateway.
UnitRole = Literal["agent-runner", "observability-station"]
Hex32 = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
ExclusionReason = Literal["paused", "offline", "operator"]


def sql_inventory_digest(image: VerifiedRelease) -> str:
    """One digest of an image's paired migration SQL (`sql_inventory`)."""
    encoded = json.dumps(sql_inventory(image), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def fleet_release(reference: ReleaseRef, sql_digest: str) -> FleetRelease:
    return FleetRelease(
        source_commit=reference.source_commit,
        schema_digest=reference.schema_digest,
        sql_inventory_digest=sql_digest,
    )


class CoordinatorEndpoint(Record):
    """Where remote unit executors reach the coordinator's listener."""

    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.:_-]+$")
    port: int = Field(ge=1, le=65535)

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"


def _sorted_keys(keys: list[tuple[str, str]], what: str) -> None:
    if keys != sorted(set(keys)):
        raise ValueError(f"{what} must be sorted and unique")


class UnitSpec(Record):
    """One included remote unit: its receipt's images and its current facts."""

    unit: UnitKey
    registry: str = Field(min_length=1, max_length=4096)
    roles: tuple[UnitRole, ...] = Field(min_length=1)
    adapter: AdapterKind
    previous: ReleaseRef
    candidate: ReleaseRef
    configuration_digest: Digest
    sql_inventory_digest: Digest
    receipt_digest: Digest
    enrollment_id: Hex32

    @field_validator("roles")
    @classmethod
    def runner_roles(cls, roles: tuple[UnitRole, ...]) -> tuple[UnitRole, ...]:
        if list(roles) != sorted(set(roles)) or "agent-runner" not in roles:
            raise ValueError("a remote unit's roles are sorted, unique and include agent-runner")
        return roles

    @model_validator(mode="after")
    def distinct_images(self) -> Self:
        if self.previous.selector == self.candidate.selector:
            raise ValueError(f"unit {self.unit.label} has no distinct candidate image")
        return self


class UnitReceipt(Record):
    """What a unit's candidate image reports about the unit (the handoff's
    `receipt` entry): everything a coordinator needs to include it.

    `platform` is provenance only; the ABI tag is the compatibility contract.
    """

    machine: str = Field(min_length=1, max_length=128)
    home: str = Field(min_length=1, max_length=4096)
    registry: str = Field(min_length=1, max_length=4096)
    roles: tuple[str, ...]
    abi: dict[str, str | None]
    platform: str = Field(max_length=256)
    adapter: AdapterKind
    previous: ReleaseRef | None
    candidate: ReleaseRef
    sql_inventory_digest: Digest
    configuration_digest: Digest
    enrollment_id: Hex32 | None

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def spec(self) -> UnitSpec:
        """This unit's place in a fleet request; refused before its first
        adoption (no selected release) or enrollment."""
        if self.previous is None:
            raise ValueError(f"unit {self.machine}:{self.home} has no selected release to leave")
        if self.enrollment_id is None:
            raise ValueError(f"unit {self.machine}:{self.home} holds no enrollment")
        return UnitSpec.model_validate(
            {
                "unit": UnitKey(machine=self.machine, home=self.home),
                "registry": self.registry,
                "roles": tuple(sorted(self.roles)),
                "adapter": self.adapter,
                "previous": self.previous,
                "candidate": self.candidate,
                "configuration_digest": self.configuration_digest,
                "sql_inventory_digest": self.sql_inventory_digest,
                "receipt_digest": self.digest,
                "enrollment_id": self.enrollment_id,
            }
        )


class Exclusion(Record):
    """A unit the operation leaves out; it stays stale until it converges."""

    unit: UnitKey
    reason: ExclusionReason
    recorded_by: str = Field(min_length=1, max_length=256)
    detail: str = Field(default="", max_length=512)


class FleetRequest(Request):
    """The coordinator's request: the gateway unit's release plus the whole fleet."""

    kind: Literal["fleet"] = "fleet"
    units: tuple[UnitSpec, ...] = ()
    excluded: tuple[Exclusion, ...] = ()
    policy: FleetPolicy = Field(default_factory=FleetPolicy)
    coordinator: CoordinatorEndpoint | None = None

    @model_validator(mode="after")
    def coherent_fleet(self) -> Self:
        included = [spec.unit.order for spec in self.units]
        excluded = [entry.unit.order for entry in self.excluded]
        _sorted_keys(included, "included units")
        _sorted_keys(excluded, "excluded units")
        if set(included) & set(excluded):
            raise ValueError("a unit is either included or excluded")
        if self.gateway.order in {*included, *excluded}:
            raise ValueError("the gateway unit is the request's own home, never a listed unit")
        if self.previous.source_commit == self.candidate.source_commit:
            raise ValueError("a fleet release moves between two distinct source commits")
        if (self.coordinator is None) != (not self.units):
            raise ValueError("a coordinator endpoint exists exactly when remote units take part")
        for spec in self.units:
            if (spec.candidate.source_commit, spec.candidate.schema_digest) != (
                self.candidate.source_commit,
                self.candidate.schema_digest,
            ) or (spec.previous.source_commit, spec.previous.schema_digest) != (
                self.previous.source_commit,
                self.previous.schema_digest,
            ):
                raise ValueError(f"unit {spec.unit.label} prepared a different release")
        return self

    @property
    def gateway(self) -> UnitKey:
        return UnitKey(machine=self.machine, home=self.home)

    def spec(self, unit: UnitKey) -> UnitSpec:
        for spec in self.units:
            if spec.unit == unit:
                return spec
        raise KeyError(unit.label)

    def unit_request(self, unit: UnitKey) -> UnitRequest:
        """The one remote unit's own home request for this operation."""
        spec = self.spec(unit)
        if self.coordinator is None:
            raise ValueError("a fleet with remote units names its coordinator endpoint")
        return UnitRequest(
            id=self.id,
            home=spec.unit.home,
            registry=spec.registry,
            created_at=self.created_at,
            machine=spec.unit.machine,
            configuration_digest=spec.configuration_digest,
            previous=spec.previous,
            candidate=spec.candidate,
            executor=spec.candidate,
            gateway=self.gateway,
            coordinator=self.coordinator,
            enrollment_id=spec.enrollment_id,
            policy=self.policy,
        )


class UnitRequest(Request):
    """One remote unit's home request: it follows the coordinator's instructions."""

    kind: Literal["unit"] = "unit"
    gateway: UnitKey
    coordinator: CoordinatorEndpoint
    enrollment_id: Hex32
    policy: FleetPolicy

    @property
    def unit(self) -> UnitKey:
        return UnitKey(machine=self.machine, home=self.home)
