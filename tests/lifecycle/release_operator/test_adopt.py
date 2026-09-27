"""`ava cluster release adopt` — first image selection for a source-run home.

Exercises the real `activate_release` + `install_steady` path-building against
a real fixture image (`conftest.build_image`), stubbing only the actual
native OS job registration (`cli.release_transition.root_service.install`) —
the same boundary `tests/lifecycle/preview/test_release_cycle_runtime.py`
already stubs for the preview's own `initial()`. Receipt *parsing* is stubbed
too: `PreparationReceipt`'s own cross-field validation is
`tests/lifecycle/preparation/test_preparation.py`'s job, not this module's.

Adoption is Linux-only (see `cli/release_operator/adopt.py`); this suite runs
on any host by patching `adopt._host_supports_adoption` for the tests that
exercise the logic past that gate — a seam kept separate from `sys.platform`
itself, since `current_abi()` (called for real inside `ReleaseRef.verify`)
also branches on the real platform and must not be faked here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import shared.cluster as cluster_pkg
from cli.release_operator import adopt as adopt_module
from cli.release_transition import root_service
from cli.release_transition.request import ReleaseRef
from shared import paths as shared_paths
from shared.os_boot_unit import BootStartAction, BootUnitContext
from shared.runtime_abi import current_abi
from shared.runtime_release import activate_release, current_pointer
from tests.lifecycle.release_operator.conftest import build_image


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(shared_paths, "ava_home", lambda: path)
    monkeypatch.setattr(cluster_pkg, "registry_path", lambda: tmp_path / "clusters.json")
    return path


def _as_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adopt_module, "_host_supports_adoption", lambda: True)


def _stub_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reference: ReleaseRef) -> Path:
    """A real (dummy-content) file at the receipt path `_adopt` reads, plus a
    stubbed parse of it — `PreparationReceipt`'s own cross-field validation is
    exercised in `tests/lifecycle/preparation/test_preparation.py`, not here."""
    fake = SimpleNamespace(
        source=SimpleNamespace(source_commit=reference.source_commit),
        image=SimpleNamespace(
            artifact_digest=reference.artifact_digest,
            manifest_digest=reference.manifest_digest,
            schema_digest=reference.schema_digest,
        ),
    )
    monkeypatch.setattr(
        adopt_module.PreparationReceipt,
        "model_validate_json",
        staticmethod(lambda _data: fake),
    )
    path = tmp_path / "receipt.json"
    path.write_bytes(b"{}")
    return path


def _stub_install(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    calls: list[tuple[BootUnitContext, BootStartAction]] = []

    def install(*, context: BootUnitContext, action: BootStartAction) -> None:
        calls.append((context, action))

    monkeypatch.setattr(root_service, "install", install)
    return calls


def test_refuses_off_linux_naming_macos_release_start(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adopt_module, "_host_supports_adoption", lambda: False)
    code = adopt_module.cmd_release_adopt(receipt=tmp_path / "receipt.json")
    assert code == 2


def test_refuses_when_a_release_is_already_selected(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    reference = build_image(home, "already-selected")
    activate_release(
        home / "releases",
        reference.artifact_digest,
        expected_current=None,
        manifest_digest=reference.manifest_digest,
        host_abi=current_abi(),
        schema_digest=reference.schema_digest,
    )
    receipt = _stub_receipt(monkeypatch, tmp_path, reference)
    code = adopt_module.cmd_release_adopt(receipt=receipt)
    assert code == 2


def test_missing_receipt_file_refuses_cleanly(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    code = adopt_module.cmd_release_adopt(receipt=tmp_path / "no-such-receipt.json")
    assert code == 2


def test_first_selection_activates_and_installs_the_steady_boot_action(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as_linux(monkeypatch)
    reference = build_image(home, "candidate")
    receipt = _stub_receipt(monkeypatch, tmp_path, reference)
    calls = _stub_install(monkeypatch)

    code = adopt_module.cmd_release_adopt(receipt=receipt)

    assert code == 0
    assert current_pointer(home / "releases") == (
        reference.artifact_digest,
        reference.manifest_digest,
    )
    assert len(calls) == 1
    context, action = calls[0]
    assert context.home == home
    assert context.registry == Path(cluster_pkg.registry_path())
    assert reference.source_commit in action.argv
