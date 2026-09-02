"""Local loaded-image/selector/receipt facts, not migration or birth permission.

No request fields, environment assertions or database expected values select the
image or receipt. The ordinary admission transaction separately checks current
publication; normal-service readiness and live schema compatibility remain separate.
"""

import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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


@dataclass(frozen=True)
class RuntimePublicationInput:
    actual: PublishedUnit
    selector: PublicationSelector
    selector_bytes_digest: str


def read_publication_selector(body: bytes) -> PublicationSelector | None:
    """An exact old selector has no receipt binding and grants no new input."""
    raw = json.loads(body)
    if isinstance(raw, dict) and set(raw) == {"artifact_digest", "manifest_digest"}:
        LegacySelector.model_validate_json(body)
        return None
    return PublicationSelector.model_validate_json(body)


def _receipt_expected(body: bytes) -> ExpectedUnitWriters:
    raw = json.loads(body)
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {"version", "expected", "services", "inventory_digest", "closure", "unresolved"}
        or type(raw["version"]) is not int
        or raw["version"] != 1
    ):
        raise ReleaseRejectedError("unsupported complete preparation receipt")
    expected = ExpectedUnitWriters.model_validate_json(json.dumps(raw["expected"]))
    if (
        not isinstance(raw["services"], list)
        or not raw["services"]
        or raw["inventory_digest"] != expected.unit().inventory_digest
        or raw["closure"] != "unknown"
        or not isinstance(raw["unresolved"], list)
        or not all(isinstance(item, str) for item in raw["unresolved"])
    ):
        raise ReleaseRejectedError("incomplete preparation receipt")
    services = [PreparedService.model_validate_json(json.dumps(item)) for item in raw["services"]]
    names = [service.session for service in services]
    if any(not name for name in names) or names != sorted(set(names)):
        raise ReleaseRejectedError("preparation service roster is empty or ambiguous")
    # This is expected inventory, not a readiness assertion. Its complete byte
    # digest binds service/gate facts without promoting unknown closure to ready.
    return expected


def resolve_runtime_publication_input() -> RuntimePublicationInput | None:
    """Resolve from this imported runtime; None is not protocol-one authority.

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
    )
