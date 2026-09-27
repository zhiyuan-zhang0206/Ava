"""`ava cluster release adopt` — first image selection for a source-run home.

Wires `shared.runtime_release.activate_release(expected_current=None)` plus
`cli.release_transition.root_service.install_steady` — exactly the sequence
`scripts/preview/release_cycle_runtime.py::initial` already exercises for the
preview's own captured bundle — to a real home/registry and a real
`PreparationReceipt` file produced by `ava cluster release prepare`. No
release-transition semantics are added: this is the same first-activation
effect, generalized off the preview's private fixture shape.

Requires a stopped root and an unselected home; anything else is `ava
cluster update --prepared`'s job (via `ava cluster release request`), not
adopt's. Linux only: the macOS root-seed-from-image action does not exist yet
(the `macos-release-start` slice) — a macOS host refuses here rather than
approximating it with the persistent home helper, which only ever starts
inside an existing release operation, never a bare first selection.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli.release_prepare.models import PreparationReceipt
from cli.release_transition.request import ReleaseRef
from shared.runtime_abi import current_abi
from shared.runtime_release import VerifiedRelease, activate_release, current_pointer
from shared.verified_file import regular_bytes


def _host_supports_adoption() -> bool:
    """A seam separate from the ABI/platform facts `current_abi()` reads, so a
    test can force this one branch without also reconfiguring host-ABI
    detection for unrelated verification calls in the same test."""
    return sys.platform == "linux"


def _adopt(receipt: Path) -> tuple[ReleaseRef, VerifiedRelease]:
    from cli.commands.root_driver import require_root_absent
    from cli.release_transition.root_service import install_steady
    from shared.cluster import registry_path
    from shared.paths import ava_home
    from shared.private_storage import ensure_private_dir

    if not _host_supports_adoption():
        raise ValueError(
            "source-run image adoption is Linux-only until the macos-release-start "
            "slice lands a macOS root-seed action"
        )
    home = ava_home()
    registry = registry_path()
    store = home / "releases"
    ensure_private_dir(store)
    try:
        parsed = PreparationReceipt.model_validate_json(regular_bytes(receipt))
    except FileNotFoundError:
        raise ValueError(f"no prepared receipt at {receipt}") from None
    if current_pointer(store) is not None:
        raise ValueError(
            "this home already has an active release selection; use `ava cluster "
            "release request` + `ava cluster update` instead"
        )
    reference = ReleaseRef(
        artifact_digest=parsed.image.artifact_digest,
        manifest_digest=parsed.image.manifest_digest,
        schema_digest=parsed.image.schema_digest,
        source_commit=parsed.source.source_commit,
    )
    image = reference.verify(home)
    require_root_absent()
    activate_release(
        store,
        reference.artifact_digest,
        expected_current=None,
        manifest_digest=reference.manifest_digest,
        host_abi=current_abi(),
        schema_digest=reference.schema_digest,
    )
    install_steady(home, registry, reference, image)
    return reference, image


def cmd_release_adopt(*, receipt: Path) -> int:
    try:
        reference, _ = _adopt(receipt)
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release adopt refused: {exc}\n")
        return 2
    sys.stdout.write(
        f"adopted commit {reference.source_commit} (artifact {reference.artifact_digest})\n"
    )
    return 0
