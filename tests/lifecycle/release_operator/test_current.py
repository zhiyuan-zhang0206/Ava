"""Read-only discovery of the currently selected release (`current_release`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.deploy.release.runtime_release import ReleaseRejectedError, activate_release
from base.runtime_abi import current_abi
from cli.release_operator.current import current_release
from cli.release_transition.request import ReleaseRef
from tests.lifecycle.release_operator.conftest import build_image, canonical


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "home"
    path.mkdir()
    return path


def _select(home: Path, reference: ReleaseRef) -> None:
    activate_release(
        home / "releases",
        reference.artifact_digest,
        expected_current=None,
        manifest_digest=reference.manifest_digest,
        host_abi=current_abi(),
        schema_digest=reference.schema_digest,
    )


def test_no_pointer_returns_none(home: Path) -> None:
    assert current_release(home) is None


def test_selected_release_is_discovered_and_reverified(home: Path) -> None:
    reference = build_image(home, "candidate")
    _select(home, reference)

    found = current_release(home)

    assert found is not None
    discovered, image = found
    assert discovered == reference
    assert image.digest == reference.artifact_digest
    assert image.manifest_digest == reference.manifest_digest


def test_tampered_manifest_refuses_rather_than_trusting_the_peeked_schema(home: Path) -> None:
    reference = build_image(home, "candidate")
    _select(home, reference)
    manifest_path = home / "releases" / reference.artifact_digest / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["schema_digest"] = "0" * 64
    manifest_path.write_bytes(canonical(manifest))

    with pytest.raises(ReleaseRejectedError):
        current_release(home)
