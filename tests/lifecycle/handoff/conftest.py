"""A home whose release store holds verifiable images with a recording interpreter.

The executor image's `venv/bin/python` is a POSIX shell script, not Python: the
handoff never inspects what it runs, only that the image verifies. Run with the
fixed v1 entry argv it records its working directory, `AVA_HOME`, its argv, the
database authority its environment carries (`AVA_DB_URL`, `AVA_DB_GENERATION`)
and (for the stdin source `-`) the exact request bytes under `$HANDOFF_RECORD`,
then prints `$HANDOFF_STDOUT` and exits `$HANDOFF_EXIT`. `$HANDOFF_SLEEP`
replaces it with `sleep` (one process, so a timeout kill leaves nothing).
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cli.release_fleet.request import FleetRequest
from cli.release_transition.request import ReleaseRef
from shared.deploy.release.runtime_release import MANIFEST_VERSION, VerifiedRelease
from shared.runtime_abi import current_abi

_SITE = "venv/lib/python3.12/site-packages"
_SCHEMA = b"CREATE TABLE example (id bigint);\n"
RECORDING_INTERPRETER = b"""#!/bin/sh
out="$HANDOFF_RECORD"
{ pwd -P; printf '%s\\n' "$AVA_HOME"; printf '%s\\n' "$@"; } > "$out.argv"
printf '%s\\n' "${AVA_DB_URL-}" "${AVA_DB_GENERATION-}" > "$out.db"
last=""
for arg in "$@"; do last="$arg"; done
if [ "$last" = "-" ]; then cat > "$out.stdin"; fi
if [ -n "$HANDOFF_SLEEP" ]; then exec sleep "$HANDOFF_SLEEP"; fi
printf '%s' "$HANDOFF_STDOUT"
printf '%s' "$HANDOFF_STDERR" >&2
exit "${HANDOFF_EXIT:-0}"
"""


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def build_image(
    home: Path,
    label: str,
    *,
    interpreter: bytes = RECORDING_INTERPRETER,
    extra: Mapping[str, bytes] | None = None,
) -> ReleaseRef:
    """A complete image under `home/releases` that `ReleaseRef.verify` accepts;
    `extra` adds members by their image-relative path."""
    artifact = _digest(label.encode())
    commit = _digest((label + "-commit").encode())[:40]
    schema = _digest(_SCHEMA)
    root = home / "releases" / artifact
    root.mkdir(parents=True)
    files = {
        "venv/bin/python": interpreter,
        f"{_SITE}/db/schema.sql": _SCHEMA,
        f"{_SITE}/shared/release-build.json": _canonical(
            {
                "version": 1,
                "source_commit": commit,
                "source_tree": "1" * 40,
                "source_archive_digest": "2" * 64,
                "schema_digest": schema,
                "applied_names": ["__baseline__"],
            }
        ),
        **(extra or {}),
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    interpreter_path = root / "venv/bin/python"
    interpreter_path.chmod(interpreter_path.stat().st_mode | stat.S_IXUSR)
    manifest = _canonical(
        {
            "version": MANIFEST_VERSION,
            "artifact_digest": artifact,
            "abi_tag": current_abi().to_json(),
            "platform": "provenance-only-platform-string",
            "schema_digest": schema,
            "interpreter": "venv/bin/python",
            "cwd": _SITE,
            "files": {name: _digest(contents) for name, contents in files.items()},
        }
    )
    (root / "manifest.json").write_bytes(manifest)
    return ReleaseRef(
        artifact_digest=artifact,
        manifest_digest=_digest(manifest),
        schema_digest=schema,
        source_commit=commit,
    )


@dataclass(frozen=True)
class Store:
    home: Path
    previous: ReleaseRef
    executor: ReleaseRef
    record: Path

    @property
    def image(self) -> VerifiedRelease:
        return self.executor.verify(self.home)

    def request(self, **overrides: Any) -> bytes:
        """A current single-host release request naming the executor image."""
        request = FleetRequest(
            id=uuid4(),
            home=str(self.home),
            registry=str(self.home.parent / "clusters.json"),
            created_at=datetime.now(UTC),
            machine="unit-a",
            previous=self.previous,
            candidate=self.executor,
            executor=self.executor,
            configuration_digest="f" * 64,
        )
        document = json.loads(request.model_dump_json())
        document.update(overrides)
        return json.dumps(document).encode()

    def recorded(self) -> list[str]:
        return (self.record.parent / f"{self.record.name}.argv").read_text().splitlines()

    def delivered(self) -> tuple[str, str]:
        """The executor's `AVA_DB_URL` and `AVA_DB_GENERATION` ('' when unset)."""
        url, generation = (self.record.parent / f"{self.record.name}.db").read_text().splitlines()
        return url, generation


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    home = tmp_path.resolve() / "home"
    home.mkdir(mode=0o700)
    record = tmp_path.resolve() / "record"
    for key in ("HANDOFF_SLEEP", "HANDOFF_EXIT", "HANDOFF_STDERR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HANDOFF_RECORD", str(record))
    monkeypatch.setenv("HANDOFF_STDOUT", '{"ok": true}')
    return Store(home, build_image(home, "previous"), build_image(home, "executor"), record)


def entry_argv_tail(entry: str, source: str) -> list[str]:
    return ["-I", "-B", "-X", "utf8", "-m", "cli.release_handoff", entry, source]
