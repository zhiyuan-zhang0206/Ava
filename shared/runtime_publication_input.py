"""Local loaded-image/selector/receipt facts, not migration or birth permission.

No request fields, environment assertions or database expected values select the
image or receipt. The ordinary admission transaction separately checks current
publication; normal-service readiness and live schema compatibility remain separate.
"""

import hashlib
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from shared.managed_writer_barrier import Digest, EvidenceModel
from shared.managed_writer_observation import ExpectedUnitWriters
from shared.managed_writer_publication import PublishedUnit
from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_venv
from shared.runtime_release import ReleaseRejectedError, file_sha256, verify_release
from shared.verified_file import regular_bytes


class PublicationSelector(EvidenceModel):
    version: Literal[2]
    artifact_digest: Digest
    manifest_digest: Digest
    inventory_receipt_digest: Digest


class LegacySelector(EvidenceModel):
    artifact_digest: Digest
    manifest_digest: Digest


class PreparedService(EvidenceModel):
    session: str
    requires_db: bool
    gate: str | None


class PreparationReceipt(EvidenceModel):
    version: Literal[1]
    expected: ExpectedUnitWriters
    services: tuple[PreparedService, ...] = Field(min_length=1)
    inventory_digest: Digest
    closure: Literal["unknown"]
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class RuntimePublicationInput:
    actual: PublishedUnit
    selector: PublicationSelector
    selector_bytes_digest: str
    loaded_prefix: Path
    process_pid: int


def read_publication_selector(body: bytes) -> PublicationSelector | None:
    """An exact old selector has no receipt binding and grants no new input."""
    raw = json.loads(body)
    if isinstance(raw, dict) and set(cast(dict[object, object], raw)) == {
        "artifact_digest",
        "manifest_digest",
    }:
        LegacySelector.model_validate_json(body)
        return None
    return PublicationSelector.model_validate_json(body)


def _receipt_expected(body: bytes) -> ExpectedUnitWriters:
    receipt = PreparationReceipt.model_validate_json(body)
    expected = receipt.expected
    if receipt.inventory_digest != expected.unit().inventory_digest:
        raise ReleaseRejectedError("incomplete preparation receipt")
    names = [service.session for service in receipt.services]
    if any(not name for name in names) or names != sorted(set(names)):
        raise ReleaseRejectedError("preparation service roster is empty or ambiguous")
    # This is expected inventory, not a readiness assertion. Its complete byte
    # digest binds service/gate facts without promoting unknown closure to ready.
    return expected


def resolve_runtime_publication_input() -> RuntimePublicationInput | None:
    """Resolve from this imported runtime; None is not protocol-one authority.

    Call once at actual process/host boot, never on each inbox claim or turn.
    Source/dev and exact legacy selectors provide no new-publication input.
    A wheel with missing/malformed metadata refuses rather than guessing.
    """
    if not WHEEL_RUNTIME:
        return None
    prefix = runtime_venv()
    root = prefix.parent
    store = root.parent
    home = store.parent
    if prefix.name != "venv" or store.name != "releases" or home.resolve(strict=True) != home:
        raise ReleaseRejectedError("loaded runtime is outside a canonical unit release")
    selector_path = store / "current-release"
    before = regular_bytes(selector_path)
    selector = read_publication_selector(before)
    if selector is None:
        return None
    if root.name != selector.artifact_digest:
        raise ReleaseRejectedError("loaded generation differs from canonical selector")
    module_root = Path(__file__).resolve().parent.parent
    if not module_root.is_relative_to(prefix):
        raise ReleaseRejectedError("publication resolver is outside the loaded environment")
    # Independent installed baseline bytes, not the manifest's own assertion.
    # This checks packaged integrity only; it does not attest applied DB migrations.
    machine_path = home / "machine_name"
    machine_bytes = regular_bytes(machine_path)
    machine = machine_bytes.decode().strip()
    if not machine:
        raise ReleaseRejectedError("installed unit machine identity is empty")
    receipt_path = home / "run" / f"release-inventory-{selector.inventory_receipt_digest}.json"
    if receipt_path.resolve(strict=True) != receipt_path:
        raise ReleaseRejectedError("preparation receipt path is not canonical")
    body = regular_bytes(receipt_path)
    if hashlib.sha256(body).hexdigest() != selector.inventory_receipt_digest:
        raise ReleaseRejectedError("complete preparation receipt digest mismatch")
    expected = _receipt_expected(body)
    if (expected.machine, expected.home, expected.artifact_digest, expected.manifest_digest) != (
        machine,
        str(home),
        selector.artifact_digest,
        selector.manifest_digest,
    ):
        raise ReleaseRejectedError("preparation receipt belongs to another unit or image")
    baseline = file_sha256(module_root / "db/schema.sql")
    image = verify_release(
        store,
        root.name,
        manifest_digest=selector.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=baseline,
    )
    if image.root != root or image.interpreter.parent.parent != prefix:
        raise ReleaseRejectedError("verified image differs from loaded runtime")
    if regular_bytes(selector_path) != before or regular_bytes(machine_path) != machine_bytes:
        raise ReleaseRejectedError("unit selector or identity changed during resolution")
    return RuntimePublicationInput(
        PublishedUnit(
            machine=machine,
            home=str(home),
            artifact_digest=image.digest,
            manifest_digest=image.manifest_digest,
            inventory_digest=hashlib.sha256(body).hexdigest(),
        ),
        selector,
        hashlib.sha256(before).hexdigest(),
        prefix,
        os.getpid(),
    )


def revalidate_runtime_publication_input(value: RuntimePublicationInput) -> None:
    """Cheap local binding check before a new hosted incarnation, not every turn.

    The object stays inside its originating process and loaded generation. This
    is not a cross-process cache or a replacement for the locked DB publication
    decision. A selector change requires fresh resolution, never reuse.
    """
    if (
        not WHEEL_RUNTIME
        or value.process_pid != os.getpid()
        or value.loaded_prefix != runtime_venv()
    ):
        raise ReleaseRejectedError("publication input belongs to another loaded process")
    root = runtime_venv().parent
    home = root.parent.parent
    if str(home) != value.actual.home or root.name != value.actual.artifact_digest:
        raise ReleaseRejectedError("publication input belongs to another unit image")
    selector = regular_bytes(root.parent / "current-release")
    if hashlib.sha256(selector).hexdigest() != value.selector_bytes_digest:
        raise ReleaseRejectedError("selector changed since runtime verification")
    if read_publication_selector(selector) != value.selector:
        raise ReleaseRejectedError("selector binding changed since runtime verification")
    if file_sha256(root / "manifest.json") != value.actual.manifest_digest:
        raise ReleaseRejectedError("loaded manifest changed since runtime verification")
    receipt = home / "run" / f"release-inventory-{value.actual.inventory_digest}.json"
    if receipt.resolve(strict=True) != receipt:
        raise ReleaseRejectedError("runtime receipt path changed")
    if hashlib.sha256(regular_bytes(receipt)).hexdigest() != value.actual.inventory_digest:
        raise ReleaseRejectedError("runtime receipt changed since verification")
    if regular_bytes(home / "machine_name").decode().strip() != value.actual.machine:
        raise ReleaseRejectedError("installed machine identity changed")
