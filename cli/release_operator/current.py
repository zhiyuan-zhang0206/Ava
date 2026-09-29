"""Read-only discovery of this home's currently selected release.

`cli.release_transition.request.ReleaseRef` is always constructed by a caller
who already knows all four fields (see `cli/release_transition/boot.py`,
`scripts/preview/release_cycle_runtime.py`): nothing in `release_transition`
reads a `ReleaseRef` back off an unlabeled generation directory. `release
request` and `release status` both need exactly that — the "previous" side of
a `Request`, or a status line, for a release nobody handed us labeled facts
for. This module discovers it, then fully verifies the discovery through the
same primitives everything else in this codebase trusts
(`shared.deploy.release.runtime_release.verify_release`, `shared.deploy.release.identity.
read_application_identity`): nothing here is trusted unverified, and no new
trust shortcut is introduced.

Never selects, activates, writes, drains or dispatches anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from cli.release_transition.request import ReleaseRef
from shared.deploy.release.identity import application_identity_members, read_application_identity
from shared.deploy.release.runtime_release import VerifiedRelease, current_pointer, verify_release
from shared.deploy.release.verified_file import regular_bytes
from shared.runtime_abi import current_abi

_MAX_MANIFEST_BYTES = 32 * 1024 * 1024


def current_release(home: Path) -> tuple[ReleaseRef, VerifiedRelease] | None:
    """The verified currently selected release, or None before any activation."""
    store = home / "releases"
    pointer = current_pointer(store)
    if pointer is None:
        return None
    artifact_digest, manifest_digest = pointer
    manifest_path = store / artifact_digest / "manifest.json"
    manifest = json.loads(regular_bytes(manifest_path, max_bytes=_MAX_MANIFEST_BYTES))
    schema_digest = manifest["schema_digest"]
    image = verify_release(
        store,
        artifact_digest,
        manifest_digest=manifest_digest,
        host_abi=current_abi(),
        schema_digest=schema_digest,
    )
    # verify_release just proved manifest_path's bytes hash to manifest_digest,
    # so the dict already parsed from those same bytes is exactly what it
    # verified — reused rather than re-read under a fresh TOCTOU window.
    members = application_identity_members(manifest["files"], manifest["abi_tag"]["os"])
    discovered_commit = json.loads(regular_bytes(image.root / members[0]))["source_commit"]
    identity = read_application_identity(image, discovered_commit)
    reference = ReleaseRef(
        artifact_digest=image.digest,
        manifest_digest=image.manifest_digest,
        schema_digest=schema_digest,
        source_commit=identity.source_commit,
    )
    return reference, image
