"""Captured registry authority must admit both release starts before any drain."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
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
    monkeypatch.setattr(cluster, "port_free", lambda _port: True)
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


class _Rows:
    def __init__(self, rows: list[tuple[str, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[str, ...]]:
        return self.rows


class _Units:
    """Exactly this home's unit and machine rows."""

    def __init__(self, home: str) -> None:
        self.home = home

    def __enter__(self) -> "_Units":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> _Rows:
        return _Rows([("unit", self.home)] if "machine_units" in sql else [("unit",)])


def _live_terminal() -> None:
    raise RuntimeError("persistent terminals/schedules ... will not kill or replay them")


def test_release_writer_boundary_admits_terminals_its_stop_phase_closes(
    prepared: Request, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live terminal is no longer a release refusal: production always has
    schedules and PTYs, and the stop phase closes them (FC-6)."""
    import shared.cluster
    import shared.db
    import shared.machine
    from cli.commands import maintenance_stop
    from cli.release_transition.identity import require_local_writers
    from shared.config import settings

    monkeypatch.setitem(os.environ, "AVA_HOME", prepared.home)
    monkeypatch.setattr(shared.machine, "machine_name", lambda: "unit")
    monkeypatch.setattr(shared.machine, "machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(shared.cluster, "registry_path", lambda: Path(prepared.registry))
    monkeypatch.setattr(type(settings.data_plane), "is_remote", property(lambda _self: False))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr(settings.data_plane, "data_plane_host", "")
    monkeypatch.setattr(shared.db, "connect", lambda: _Units(prepared.home))
    monkeypatch.setattr(maintenance_stop, "require_no_terminals", _live_terminal)
    require_local_writers(prepared)


def test_pitr_still_refuses_a_live_terminal_before_draining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PITR is not a release boundary: it keeps refusing retained terminals."""
    from cli.commands import maintenance_stop
    from cli.release_transition import identity
    from cli.release_transition.pitr import transition
    from shared import runtime_release

    driver = object.__new__(transition.PitrTransition)
    driver.request = SimpleNamespace(  # type: ignore[assignment]
        require_configuration=lambda: None, image=SimpleNamespace(selector="selected")
    )
    driver.home = tmp_path

    def selected(_store: Path) -> str:
        return "selected"

    def one_host(_request: object) -> None:
        return None

    monkeypatch.setattr(runtime_release, "current_pointer", selected)
    monkeypatch.setattr(identity, "require_local_writers", one_host)
    monkeypatch.setattr(maintenance_stop, "require_no_terminals", _live_terminal)
    with pytest.raises(RuntimeError, match="will not kill or replay"):
        driver.preflight()
