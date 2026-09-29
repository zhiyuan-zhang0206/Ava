"""Shared fixture-building helper for the release-operator verb tests.

`build_image` writes one real, independently verifiable release generation
under `<home>/releases/<artifact_digest>` — the same minimal shape
`tests/lifecycle/transition/test_request.py::_image` builds for
`verify_pair` — so `cli.release_operator.current.current_release`,
`ReleaseRef.verify` and `activate_release` all run for real against it rather
than against a mock. No native image assembly, database or OS job is
involved; the tests that install a boot action still stub the actual native
registration call (see `tests/lifecycle/preview/test_release_cycle_runtime.py`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from base.deploy.release.runtime_release import MANIFEST_VERSION
from base.runtime_abi import current_abi
from cli.release_transition.request import ReleaseRef

_SITE = "venv/lib/python3.12/site-packages"
_BASELINE = b"SELECT 1;\n"
# `cli.release_transition.request.sql_inventory` (used by `verify_pair`) refuses any
# image with zero migration files, even a self-consistent one — every fixture
# image needs at least one, and every `build_image` call uses this same fixed
# content, so any two fixture images already agree (the happy-path case).
_MIGRATION_NAME = "20260101T000000_baseline.sql"
_MIGRATION_SQL = b"SELECT 1;\n"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def build_image(home: Path, label: str, *, schema: bytes = _BASELINE) -> ReleaseRef:
    """Write one real, verifiable generation under `home/releases`; no CAS pointer."""
    artifact = digest(label.encode())
    commit = digest((label + "-commit").encode())[:40]
    schema_digest = digest(schema)
    root = home / "releases" / artifact
    files = {
        "venv/bin/python": b"inert interpreter fixture\n",
        f"{_SITE}/db/schema.sql": schema,
        f"{_SITE}/migrations/{_MIGRATION_NAME}": _MIGRATION_SQL,
        f"{_SITE}/shared/release-build.json": canonical(
            {
                "version": 1,
                "source_commit": commit,
                "source_tree": "1" * 40,
                "source_archive_digest": "2" * 64,
                "schema_digest": schema_digest,
                "applied_names": ["__baseline__"],
            }
        ),
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    manifest = canonical(
        {
            "version": MANIFEST_VERSION,
            "artifact_digest": artifact,
            "abi_tag": current_abi().to_json(),
            "platform": "release-operator-test-fixture",
            "schema_digest": schema_digest,
            "interpreter": "venv/bin/python",
            "cwd": _SITE,
            "files": {name: digest(contents) for name, contents in files.items()},
        }
    )
    (root / "manifest.json").write_bytes(manifest)
    return ReleaseRef(
        artifact_digest=artifact,
        manifest_digest=digest(manifest),
        schema_digest=schema_digest,
        source_commit=commit,
    )
