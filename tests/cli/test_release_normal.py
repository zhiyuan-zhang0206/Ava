"""Normal updater producer contracts; actual cold launch remains a CI gate."""

from pathlib import Path

import pytest

from cli.commands._release_selector import read_selector, selector_bytes
from cli.commands._release_services import _command
from ops.spec import ServiceSpec
from shared.managed_writer_publication import CandidateUnitPlan, NormalService, PublishedUnit
from shared.runtime_publication_input import read_publication_selector
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease


def test_selector_writer_and_pending_plan_share_exact_bytes(tmp_path: Path) -> None:
    import hashlib

    unit = PublishedUnit(
        machine="test",
        home=str(tmp_path),
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        inventory_digest="c" * 64,
    )
    encoded = selector_bytes(unit)
    selector = read_publication_selector(encoded)
    assert selector is not None and selector.inventory_receipt_digest == unit.inventory_digest
    service = NormalService(
        session="ava-ops",
        module="services.agent_ops.daemon",
        executable=str(tmp_path / "releases" / unit.artifact_digest / "python/bin/python"),
        entrypoint=str(tmp_path / "releases" / unit.artifact_digest / "venv/ops.py"),
        command_digest="d" * 64,
    )
    plan = CandidateUnitPlan(
        unit=unit,
        services=(service,),
        previous_selector_digest=None,
        selector_digest=hashlib.sha256(encoded).hexdigest(),
    )
    assert plan.unit == unit


def test_selector_reader_refuses_symlink_ancestor(tmp_path: Path) -> None:
    actual = tmp_path / "unit"
    (actual / "releases").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ReleaseRejectedError, match="canonical"):
        read_selector(alias)


def test_unknown_native_service_rejected_before_start(tmp_path: Path) -> None:
    root = tmp_path / "image"
    root.mkdir()
    executable = root / "unknown-service"
    executable.write_bytes(b"not executed")
    image = VerifiedRelease("a" * 64, "b" * 64, root, executable, root)
    spec = ServiceSpec(
        session="unknown",
        cmd=str(executable),
        capabilities=frozenset({"gateway"}),
        requires_db=False,
        curl_url="http://127.0.0.1:12345/healthz",
    )
    with pytest.raises(ReleaseRejectedError, match="readiness adapter"):
        _command(spec, image)


def test_mutable_executable_rejected_before_probe(tmp_path: Path) -> None:
    root = tmp_path / "image"
    root.mkdir()
    executable = tmp_path / "mutable"
    executable.write_bytes(b"not executed")
    image = VerifiedRelease("a" * 64, "b" * 64, root, executable, root)
    spec = ServiceSpec(
        session="otel-collector",
        cmd=str(executable),
        capabilities=frozenset({"gateway"}),
        requires_db=False,
    )
    with pytest.raises(ReleaseRejectedError, match="outside"):
        _command(spec, image)


@pytest.mark.parametrize(
    ("generation", "stage"),
    [
        ("replacement", "candidate_ready"),
        ("expected", "recovered"),
        ("expected", "candidate_started"),
    ],
)
def test_continuation_requires_same_actual_ready_handoff(
    monkeypatch: pytest.MonkeyPatch, generation: str, stage: str
) -> None:
    import json
    from unittest.mock import Mock

    from cli.commands import _update_normal_release as normal
    from cli.commands._update_bootstrap import PreparedBootstrapHop

    payload = json.dumps({"generation": generation, "bootstrap_hop": {"stage": stage}}).encode()

    def read_payload(_path: Path) -> bytes:
        return payload

    monkeypatch.setattr(normal, "regular_bytes", read_payload)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("exit code alone must not authorize normal probing or service effects")

    monkeypatch.setattr(normal, "probe_bootstrap", forbidden)
    monkeypatch.setattr(normal, "execute_normal_release", forbidden)
    with pytest.raises(ReleaseRejectedError, match="actual candidate-ready handoff"):
        normal.continue_after_bootstrap(
            Mock(spec=PreparedBootstrapHop), Mock(spec=normal.PreparedNormalRelease), "expected"
        )


def test_prepared_services_pin_dependency_order_before_mutable_roster_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import Mock

    from cli.commands import _release_services as services
    from shared.runtime_publication_input import PreparedService as ReceiptService

    root = tmp_path / "releases" / ("a" * 64)
    image = VerifiedRelease("a" * 64, "b" * 64, root, root / "python", root)
    unit = PublishedUnit(
        machine="test",
        home=str(tmp_path),
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        inventory_digest="c" * 64,
    )
    specs = [
        ServiceSpec(
            session=name, cmd="unused", capabilities=frozenset({"gateway"}), requires_db=False
        )
        for name in ("z-dependency", "a-consumer", "disabled")
    ]
    roster = [(specs[0], None), (specs[1], None), (specs[2], "disabled")]
    receipt = Mock(
        services=tuple(
            sorted(
                (
                    ReceiptService(session=spec.session, requires_db=False, gate=gate)
                    for spec, gate in roster
                ),
                key=lambda item: item.session,
            )
        )
    )

    def verified(_unit: PublishedUnit, _schema: str) -> VerifiedRelease:
        return image

    def prefix() -> Path:
        return root / "venv"

    def read(_path: Path) -> bytes:
        return b"receipt parsed by boundary fake"

    def parsed(_body: bytes) -> object:
        return receipt

    def discover(_roles: object) -> list[tuple[ServiceSpec, str | None]]:
        return roster

    def roles() -> frozenset[str]:
        return frozenset({"gateway"})

    def command(spec: ServiceSpec, _image: VerifiedRelease) -> services.PreparedService:
        identity = NormalService(
            session=f"ava-{spec.session}",
            module=None,
            executable=str(root / "binary"),
            entrypoint=str(root / "binary"),
            command_digest="d" * 64,
        )
        return services.PreparedService(identity, spec, ("unused",), root, {})

    monkeypatch.setattr(services, "verify_unit_image", verified)
    monkeypatch.setattr(services, "runtime_venv", prefix)
    monkeypatch.setattr(services, "regular_bytes", read)
    monkeypatch.setattr(services.PreparationReceipt, "model_validate_json", parsed)
    monkeypatch.setattr(services, "services_for_capabilities_annotated", discover)
    monkeypatch.setattr(services, "machine_role", roles)
    monkeypatch.setattr(services, "_command", command)
    prepared = services.prepare_normal_services(unit, "e" * 64)
    roster.clear()
    assert [item.identity.session for item in prepared] == ["ava-z-dependency", "ava-a-consumer"]
