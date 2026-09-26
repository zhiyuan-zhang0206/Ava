"""Captured registry authority must admit both release starts before any drain."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from cli.release_transition.identity import require_reservation
from cli.release_transition.request import ReleaseRef, Request
from cli.start_identity import IdentityInput, mark_phase, prepare_identity
from shared import cluster


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Request:
    root = tmp_path.resolve()
    checkout = root / "source"
    checkout.mkdir()
    home, registry = root / "home", root / "registry.json"
    monkeypatch.setattr(cluster, "_port_free", lambda _port: True)
    prepare_identity(
        IdentityInput(
            home,
            registry,
            checkout,
            False,
            frozenset({"gateway", "agent-runner"}),
            {"AVA_MACHINE_NAME": "unit"},
        )
    )
    mark_phase(home, "provisioned")
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64})
    return Request(
        id=uuid4(),
        home=str(home),
        registry=str(registry),
        created_at=datetime.now(UTC),
        platform_tag="Linux-test",
        machine="unit",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest="f" * 64,
    )


def test_matching_prepared_reservation_does_not_rewrite_it(prepared: Request) -> None:
    registry = Path(prepared.registry)
    before = registry.read_bytes()
    require_reservation(prepared, active_registry=registry)
    assert registry.read_bytes() == before


def test_request_cannot_redirect_runtime_to_an_empty_registry(prepared: Request) -> None:
    wrong = Path(prepared.registry).with_name("other.json")
    wrong.write_text("{}")
    redirected = prepared.model_copy(update={"registry": str(wrong)})
    with pytest.raises(ValueError, match="active registry authority"):
        require_reservation(redirected, active_registry=Path(prepared.registry))
    with pytest.raises(ValueError, match="persisted start identity"):
        require_reservation(redirected, active_registry=wrong)


@pytest.mark.parametrize("mutation", ["missing", "ports", "home"])
def test_lost_or_changed_reservation_refuses_before_drain(prepared: Request, mutation: str) -> None:
    registry = Path(prepared.registry)
    values = json.loads(registry.read_bytes())
    if mutation == "missing":
        values.clear()
    elif mutation == "ports":
        ports = values[prepared.home]["ports"]
        name = next(iter(ports))
        ports[name] += 1
    else:
        values[prepared.home]["gateway_home"] = str(Path(prepared.home).with_name("other"))
    registry.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="persisted start identity"):
        require_reservation(prepared, active_registry=registry)


def test_registry_symlink_is_not_an_admitted_authority(prepared: Request) -> None:
    link = Path(prepared.registry).with_name("registry-link.json")
    link.symlink_to(prepared.registry)
    redirected = prepared.model_copy(update={"registry": str(link)})
    with pytest.raises(ValueError, match="active registry authority"):
        require_reservation(redirected, active_registry=link)
