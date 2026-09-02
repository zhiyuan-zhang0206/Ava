"""CI-only consumer of a real installed image and producer's complete receipt."""

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shared.managed_writer_observation import ExpectedUnitWriters
from shared.runtime_publication_input import (
    resolve_runtime_publication_input,
    revalidate_runtime_publication_input,
)
from shared.runtime_release import ReleaseRejectedError


def prove_publication_input(home: Path, receipt: Path) -> None:  # noqa: PLR0915 — isolated selector/receipt lifetime, always restored.
    """Called inside the existing source-absent installed inventory proof."""
    original_receipt = receipt.read_bytes()
    expected = json.loads(original_receipt)["expected"]
    selector = home / "releases/current-release"
    original_selector = selector.read_bytes() if selector.exists() else None
    chosen = {
        "version": 2,
        "artifact_digest": expected["artifact_digest"],
        "manifest_digest": expected["manifest_digest"],
        "inventory_receipt_digest": hashlib.sha256(original_receipt).hexdigest(),
    }
    try:
        # Test-only publication bytes, not production activation or closure.
        selector.write_text(json.dumps(chosen))
        actual = resolve_runtime_publication_input()
        if (
            actual is None
            or actual.actual.home != str(home)
            or actual.actual.inventory_digest != chosen["inventory_receipt_digest"]
        ):
            raise AssertionError("installed resolver did not return actual complete receipt")
        with patch(
            "shared.runtime_publication_input.verify_release", side_effect=AssertionError("rehash")
        ):
            revalidate_runtime_publication_input(actual)
            try:
                revalidate_runtime_publication_input(
                    replace(actual, process_pid=actual.process_pid + 1)
                )
            except ReleaseRejectedError:
                pass
            else:
                raise AssertionError("runtime input reused across processes")
        for mutation in ("selector", "receipt", "home"):
            other: Path | None = None
            if mutation == "selector":
                selector.write_text(json.dumps(chosen | {"artifact_digest": "0" * 64}))
            elif mutation == "receipt":
                receipt.write_bytes(original_receipt + b" ")
            else:
                wrong = json.loads(original_receipt)
                wrong["expected"]["home"] = "/wrong-home"
                wrong["inventory_digest"] = (
                    ExpectedUnitWriters.model_validate_json(json.dumps(wrong["expected"]))
                    .unit()
                    .inventory_digest
                )
                changed = json.dumps(wrong, sort_keys=True, separators=(",", ":")).encode()
                digest = hashlib.sha256(changed).hexdigest()
                other = home / "run" / f"release-inventory-{digest}.json"
                other.write_bytes(changed)
                selector.write_text(json.dumps(chosen | {"inventory_receipt_digest": digest}))
            try:
                try:
                    revalidate_runtime_publication_input(actual)
                except ReleaseRejectedError:
                    pass
                else:
                    raise AssertionError("cheap runtime binding accepted changed input")
                resolve_runtime_publication_input()
            except (ReleaseRejectedError, ValueError):
                pass
            else:
                raise AssertionError(f"publication resolver accepted {mutation} mismatch")
            finally:
                receipt.write_bytes(original_receipt)
                selector.write_text(json.dumps(chosen))
                if other is not None:
                    other.unlink()
        selector.write_text(
            json.dumps({key: chosen[key] for key in ("artifact_digest", "manifest_digest")})
        )
        if resolve_runtime_publication_input() is not None:
            raise AssertionError("legacy selector granted publication input")
        (home.parent / "runtime-publication-input-proof.json").write_text(
            json.dumps(
                {
                    "installedSourceAbsent": True,
                    "actualCompleteReceipt": True,
                    "selectorChangeRejected": True,
                    "receiptTamperingRejected": True,
                    "wrongHomeRejected": True,
                    "legacyGrantsNoNewInput": True,
                    "liveSchemaCompatibilityProved": False,
                    "publicationActivated": False,
                    "cheapBindingAvoidsImageRehash": True,
                    "crossProcessReuseRejected": True,
                }
            )
        )
    finally:
        receipt.write_bytes(original_receipt)
        if original_selector is None:
            selector.unlink(missing_ok=True)
        else:
            selector.write_bytes(original_selector)


if __name__ == "__main__":
    prove_publication_input(Path(sys.argv[1]), Path(sys.argv[2]))
