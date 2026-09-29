"""`ava cluster release adopt` — first image selection for a source-run home.

Wires `shared.deploy.release.runtime_release.activate_release(expected_current=None)` plus
`cli.release_transition.root_service.install_steady` — exactly the sequence
`scripts/preview/release_cycle_runtime.py::initial` already exercises for the
preview's own captured bundle — to a real home/registry and a real
`PreparationReceipt` file produced by `ava cluster release prepare`. No
release-transition semantics are added: this is the same first-activation
effect, generalized off the preview's private fixture shape.

Requires a stopped root and a home that selects no other release; anything
else is `ava cluster update --prepared`'s job (via `ava cluster release
request`), not adopt's. The selection commits before the boot action is
installed, so a failure between the two (`sudo -n` wanting a password, a
systemd error) or a crash leaves the receipt's own selection: re-running
adopt with the same receipt finishes the install instead of refusing, and
nothing else has to undo the selection. A release operation that holds
startup refuses adopt as it refuses `ava start`: an operation that activated
its candidate leaves the same pointer, and its boot action is not adopt's to
replace. Both run under the home's start
intent and lifecycle locks, in the order `ava start` takes them. Linux only: the macOS root-seed-from-image action does not exist yet
(the `macos-release-start` slice) — a macOS host refuses here rather than
approximating it with the persistent home helper, which only ever starts
inside an existing release operation, never a bare first selection.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli.release_prepare.models import PreparationReceipt
from cli.release_transition.request import ReleaseRef
from shared.deploy.release.runtime_release import VerifiedRelease, activate_release, current_pointer
from shared.deploy.release.verified_file import regular_bytes
from shared.runtime_abi import current_abi


def _host_supports_adoption() -> bool:
    """A seam separate from the ABI/platform facts `current_abi()` reads, so a
    test can force this one branch without also reconfiguring host-ABI
    detection for unrelated verification calls in the same test."""
    return sys.platform == "linux"


def _adopt(receipt: Path) -> tuple[ReleaseRef, VerifiedRelease]:
    from shared.deploy.lifecycle.home_lifecycle_locks import resource_lock
    from shared.host.private_storage import ensure_private_dir
    from shared.paths import ava_home
    from shared.platform import file_lock

    if not _host_supports_adoption():
        raise ValueError(
            "source-run image adoption is Linux-only until the macos-release-start "
            "slice lands a macOS root-seed action"
        )
    home = ava_home()
    try:
        parsed = PreparationReceipt.model_validate_json(regular_bytes(receipt))
    except FileNotFoundError:
        raise ValueError(f"no prepared receipt at {receipt}") from None
    reference = ReleaseRef(
        artifact_digest=parsed.image.artifact_digest,
        manifest_digest=parsed.image.manifest_digest,
        schema_digest=parsed.image.schema_digest,
        source_commit=parsed.source.source_commit,
    )
    ensure_private_dir(home)
    with (
        file_lock(home / "start-intent.lock", timeout_s=30),
        resource_lock(purpose="cli.release_adopt"),
    ):
        return reference, _select_and_install(home, reference, receipt)


def _select_and_install(home: Path, reference: ReleaseRef, receipt: Path) -> VerifiedRelease:
    from cli.commands.root_driver import require_root_absent
    from cli.release_transition.root_service import install_steady
    from shared.cluster import registry_path
    from shared.deploy.release.operation import require_start_authorized
    from shared.host.private_storage import ensure_private_dir

    # An operation that activated its candidate leaves the pointer on this
    # receipt's image, which the re-run below would otherwise accept; its boot
    # action is the operation's to install. Refuse exactly as `ava start` does.
    require_start_authorized(home)
    store = home / "releases"
    ensure_private_dir(store)
    current = current_pointer(store)
    if current not in (None, (reference.artifact_digest, reference.manifest_digest)):
        raise ValueError(
            "this home already selects another release; use `ava cluster release "
            "request` + `ava cluster update` instead"
        )
    image = reference.verify(home)
    require_root_absent()
    if current is None:
        activate_release(
            store,
            reference.artifact_digest,
            expected_current=None,
            manifest_digest=reference.manifest_digest,
            host_abi=current_abi(),
            schema_digest=reference.schema_digest,
        )
    try:
        install_steady(home, registry_path(), reference, image)
    except (ValueError, OSError, RuntimeError) as exc:
        raise RuntimeError(
            f"{exc}; the selection of commit {reference.source_commit} stands, so fix the "
            f"cause and re-run `ava cluster release adopt --receipt {receipt}` to finish"
        ) from exc
    return image


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
