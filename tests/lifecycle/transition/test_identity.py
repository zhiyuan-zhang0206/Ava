"""The home's own start intent must admit a release before any drain."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from base import cluster
from cli.release_fleet.request import FleetRequest
from cli.release_transition.identity import require_reservation
from cli.release_transition.request import ReleaseRef
from cli.start_identity import INTENT_NAME, IdentityInput, mark_phase, prepare_identity


def _request(home: Path) -> FleetRequest:
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64, "source_commit": "9" * 40})
    return FleetRequest(
        id=uuid4(),
        home=str(home),
        created_at=datetime.now(UTC),
        machine="unit",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest="f" * 64,
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A born gateway home, not yet provisioned."""
    root = tmp_path.resolve()
    checkout = root / "source"
    checkout.mkdir()
    home = root / "home"
    monkeypatch.setattr(cluster, "port_free", lambda _port: True)
    prepare_identity(
        IdentityInput(
            home,
            checkout,
            False,
            frozenset({"gateway", "agent-runner"}),
            {"AVA_MACHINE_NAME": "unit"},
        )
    )
    return home


def test_initialized_gateway_home_is_admitted_without_rewriting_it(home: Path) -> None:
    mark_phase(home, "provisioned")
    intent = home / INTENT_NAME
    before = intent.read_bytes()
    require_reservation(_request(home))
    assert intent.read_bytes() == before


def test_unprovisioned_home_refuses_before_drain(home: Path) -> None:
    with pytest.raises(ValueError, match="initialized gateway reservation"):
        require_reservation(_request(home))


def test_home_without_a_start_intent_refuses_before_drain(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="initialized gateway reservation"):
        require_reservation(_request(tmp_path.resolve() / "never-born"))
