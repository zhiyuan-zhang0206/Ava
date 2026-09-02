"""Read-only unit inventory for preparation, never a managed-writer closure permit.

All session records are retained, including disabled and no-longer-declared
services. OS registrations are collected separately from the desired roster.
Unknown ownership and unsupported platforms refuse before a receipt is written.
"""

from __future__ import annotations

import hashlib
import json
import platform
import plistlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from shared.managed_writer_observation import (
    ExpectedLauncher,
    ExpectedProcess,
    ExpectedSession,
    ExpectedUnitWriters,
)
from shared.native_job_observation import (
    read_crontab,
    read_launchd_definition,
    read_launchd_labels,
)
from shared.private_storage import write_private_bytes
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, verify_release
from shared.verified_file import regular_bytes as _regular_bytes


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sessions(home: Path) -> tuple[ExpectedSession, ...]:
    directory = home / "run/sessions"
    if directory.resolve(strict=True) != directory:
        raise ReleaseRejectedError("session inventory directory is not canonical")
    result: list[ExpectedSession] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            raise ReleaseRejectedError("unclassified session inventory member")
        raw = json.loads(_regular_bytes(path))
        result.append(
            ExpectedSession(
                name=path.stem,
                process=ExpectedProcess(
                    pid=raw["pid"],
                    create_time=raw["create_time"],
                    starttime=raw.get("starttime"),
                ),
                generation=raw.get("generation"),
            )
        )
    if not result:
        raise ReleaseRejectedError("empty session inventory cannot prove a serving unit")
    return tuple(result)


def _launchd(home: Path) -> tuple[ExpectedLauncher, ...]:
    directory = Path.home() / "Library/LaunchAgents"
    if directory.resolve(strict=True) != directory:
        raise ReleaseRejectedError("launchd inventory directory is not canonical")
    result: list[ExpectedLauncher] = []
    for path in sorted(directory.glob("*.plist")):
        encoded = _regular_bytes(path)
        raw = plistlib.loads(encoded)
        label = raw["Label"]
        if not isinstance(label, str):
            raise ReleaseRejectedError("invalid launchd label")
        if not label.startswith("com.ava."):
            continue
        if path.name != f"{label}.plist" or read_launchd_definition(label) != encoded:
            raise ReleaseRejectedError("launchd definition identity changed")
        environment = raw.get("EnvironmentVariables", {})
        # Legacy labels with no explicit home cannot be silently classified as
        # another unit. Their ownership requires an explicit migration first.
        if environment.get("AVA_HOME") != str(home):
            raise ReleaseRejectedError("Ava launchd registration has unknown or other unit home")
        result.append(
            ExpectedLauncher(
                kind="launchd", name=label, definition_digest=hashlib.sha256(encoded).hexdigest()
            )
        )
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    before = read_launchd_labels(deadline)
    after = read_launchd_labels(deadline)
    if before != after:
        raise ReleaseRejectedError("loaded launcher inventory changed")
    loaded = {label for label in after if label.startswith("com.ava.")}
    if not loaded <= {item.name for item in result}:
        raise ReleaseRejectedError("loaded Ava job has no inventoried definition")
    return tuple(result)


def _launchers(home: Path) -> tuple[ExpectedLauncher, ...]:
    if sys.platform == "darwin":
        return _launchd(home)
    if sys.platform != "linux":
        raise ReleaseRejectedError("unit launcher inventory platform is unsupported")
    body = read_crontab(datetime.now(UTC) + timedelta(seconds=10)).decode("utf-8")
    result: list[ExpectedLauncher] = []
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "ava" not in line.lower():
            continue
        if str(home) not in line or "# ava-" not in line:
            raise ReleaseRejectedError("unclassified or legacy Ava cron registration")
        digest = hashlib.sha256(line.encode()).hexdigest()
        result.append(ExpectedLauncher(kind="crontab", name=digest, definition_digest=digest))
    return tuple(result)


def _service_roster() -> list[dict[str, object]]:
    from ops.spec import services_for_capabilities_annotated
    from shared.machine import machine_role
    from shared.runtime_interpreter import WHEEL_RUNTIME

    if not WHEEL_RUNTIME:
        raise ReleaseRejectedError("inventory must load the verified candidate service code")
    roster: list[dict[str, object]] = [
        {"session": spec.session, "requires_db": spec.requires_db, "gate": reason}
        for spec, reason in services_for_capabilities_annotated(machine_role())
    ]
    if not roster or len({str(row["session"]) for row in roster}) != len(roster):
        raise ReleaseRejectedError("empty or conflicting candidate service roster")
    return sorted(roster, key=lambda row: str(row["session"]))


def collect_inventory(
    conn: psycopg.Connection,
    release: VerifiedRelease,
    home: Path,
    machine: str,
    *,
    schema_digest: str,
) -> dict[str, object]:
    """Collect actual facts twice; no writes, stop, plugin install or activation."""
    if not home.is_absolute() or home.resolve(strict=True) != home:
        raise ReleaseRejectedError("inventory requires the canonical installed unit home")
    if (home / "machine_name").read_text().strip() != machine:
        raise ReleaseRejectedError("installed unit identity changed")
    verified = verify_release(
        release.root.parent,
        release.digest,
        manifest_digest=release.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    if verified != release:
        raise ReleaseRejectedError("inventory candidate verification changed")
    if not Path(__file__).resolve().is_relative_to(release.root / "venv"):
        raise ReleaseRejectedError("inventory code is not loaded from the candidate")
    unit = conn.execute(
        "SELECT home FROM machine_units WHERE machine_name=%s AND home=%s", (machine, str(home))
    ).fetchone()
    if unit != (str(home),):
        raise ReleaseRejectedError("inventory unit is not registered")
    sessions = _sessions(home)
    launchers = _launchers(home)
    if not launchers:
        raise ReleaseRejectedError("empty launcher inventory is not complete coverage")
    # Exact recorded processes are stable across separate prepare/revalidate
    # invocations. The collector itself is not an old writer incarnation.
    processes = tuple({entry.process.pid: entry.process for entry in sessions}.values())
    expected = ExpectedUnitWriters(
        machine=machine,
        home=str(home),
        artifact_digest=release.digest,
        manifest_digest=release.manifest_digest,
        processes=processes,
        sessions=sessions,
        launchers=launchers,
    )
    roster = _service_roster()
    final_unit = conn.execute(
        "SELECT home FROM machine_units WHERE machine_name=%s AND home=%s", (machine, str(home))
    ).fetchone()
    if (
        final_unit != unit
        or (home / "machine_name").read_text().strip() != machine
        or sessions != _sessions(home)
        or launchers != _launchers(home)
        or roster != _service_roster()
    ):
        raise ReleaseRejectedError("unit inventory changed during preparation")
    return {
        "version": 1,
        "expected": expected.model_dump(mode="json"),
        "services": roster,
        "inventory_digest": expected.unit().inventory_digest,
        "closure": "unknown",
        "unresolved": [
            "non-session managed processes and predecessor orchestrator",
            "system-level or alternate-user relaunchers",
            "positive platform launcher shutdown observation",
        ],
    }


def _write_prepared_inventory(home: Path, inventory: dict[str, object]) -> Path:
    """Seal expected facts outside the image; this is not post-stop collection."""
    expected = ExpectedUnitWriters.model_validate_json(_canonical(inventory["expected"]))
    if expected.home != str(home) or home.resolve(strict=True) != home:
        raise ReleaseRejectedError("prepared inventory belongs to a different unit")
    encoded = _canonical(inventory)
    digest = hashlib.sha256(encoded).hexdigest()
    directory = home / "run"
    if directory.resolve(strict=True) != directory:
        raise ReleaseRejectedError("prepared inventory directory is not canonical")
    path = directory / f"release-inventory-{digest}.json"
    if path.exists():
        if _regular_bytes(path) != encoded:
            raise ReleaseRejectedError("prepared inventory receipt changed")
    else:
        write_private_bytes(path, encoded)
    return path


def prepare_unit_inventory(
    conn: psycopg.Connection,
    release: VerifiedRelease,
    home: Path,
    machine: str,
    *,
    schema_digest: str,
) -> Path:
    """Real preparation entry: collect verified facts before sealing a receipt.

    The whole receipt's digest, not only expected.unit().inventory_digest, must
    bind later collection/adoption so a changed service with no PID cannot alias.
    This receipt deliberately retains unresolved coverage and cannot enter
    maintenance. It is not the once-only post-stop candidate receipt.
    """
    inventory = collect_inventory(conn, release, home, machine, schema_digest=schema_digest)
    return _write_prepared_inventory(home, inventory)


def revalidate_prepared_inventory(
    conn: psycopg.Connection,
    release: VerifiedRelease,
    home: Path,
    machine: str,
    path: Path,
    *,
    schema_digest: str,
) -> ExpectedUnitWriters:
    """Reject omitted/changed facts using the actual producer, not caller flags."""
    if path.parent != home / "run" or path.resolve(strict=True) != path:
        raise ReleaseRejectedError("inventory receipt is outside this unit")
    encoded = _regular_bytes(path)
    if path.name != f"release-inventory-{hashlib.sha256(encoded).hexdigest()}.json":
        raise ReleaseRejectedError("inventory receipt digest mismatch")
    current = collect_inventory(conn, release, home, machine, schema_digest=schema_digest)
    if encoded != _canonical(current):
        raise ReleaseRejectedError("prepared unit inventory no longer matches actual facts")
    return ExpectedUnitWriters.model_validate_json(_canonical(current["expected"]))


def assert_inventory_can_enter_maintenance(path: Path) -> None:
    """No consumer can reinterpret this bounded inventory as complete closure."""
    # This implementation has no complete non-session/OS-relauncher producer.
    # Adding one requires actual platform proof, not clearing a JSON flag.
    _regular_bytes(path)
    raise ReleaseRejectedError("managed writer coverage is unresolved; maintenance is forbidden")
