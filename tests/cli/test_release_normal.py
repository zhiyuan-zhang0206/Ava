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
