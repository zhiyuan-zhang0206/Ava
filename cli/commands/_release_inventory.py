"""Read-only unit inventory for preparation, never a managed-writer closure permit.

All session records are retained, including disabled and no-longer-declared
services. OS registrations are collected separately from the desired roster.
Machine-level registrations, this home's keeper, and other installed unit homes
are classified and recorded;
unknown ownership and unsupported platforms refuse before a receipt is written.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import psycopg

from shared.managed_writer_observation import (
    ExcludedRegistration,
    ExpectedLauncher,
    ExpectedSession,
    ExpectedUnitWriters,
)
from shared.native_job_observation import (
    read_crontab,
    read_launchd_definition,
    read_launchd_labels,
)
from shared.private_storage import write_private_bytes
from shared.process_evidence import ExpectedProcess
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


def _canonical_owned_directory(path: Path) -> bool:
    return path.resolve(strict=True) == path and path.is_dir() and path.stat().st_uid == os.getuid()


def _homes_overlap(path: Path, home: Path) -> bool:
    # Path.resolve preserves spelling on case-insensitive filesystems. Compare
    # filesystem identity along both ancestor chains, not only lexical paths.
    return any(path.samefile(parent) for parent in (home, *home.parents)) or any(
        home.samefile(parent) for parent in path.parents
    )


def _is_other_unit_home(value: object, home: Path) -> bool:
    """Positive independent installed ownership; aliases and overlapping homes refuse."""
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        return False
    try:
        if not _canonical_owned_directory(path) or _homes_overlap(path, home):
            return False
        return bool(_regular_bytes(path / "machine_name").decode("utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def _keeper_role(socket: object, home: Path) -> Literal["keeper", "other-unit"]:
    if not isinstance(socket, str):
        raise ReleaseRejectedError("permissions helper registration socket is malformed")
    path = PurePosixPath(socket)
    if not path.is_absolute() or ".." in path.parts or str(path) != socket:
        raise ReleaseRejectedError("permissions helper registration socket is malformed")
    if socket.startswith(f"{home}/run/permissions-helper."):
        return "keeper"
    if (
        path.parent.name == "run"
        and path.name.startswith("permissions-helper.")
        and path.name.endswith(".sock")
        and _is_other_unit_home(str(path.parent.parent), home)
        and _canonical_owned_directory(Path(path.parent))
    ):
        return "other-unit"
    raise ReleaseRejectedError("permissions helper registration belongs to another home")


def _registration_role(
    environment: object,
    home: Path,
) -> Literal["unit", "machine", "keeper", "other-unit"]:
    """Classify one com.ava.* registration from its declared environment.

    Exactly one declaration decides the role: this home's AVA_HOME for a unit
    launcher, AVA_JOB_SCOPE=machine for a machine-level registration, or this
    home's AVA_PERMISSIONS_HELPER_SOCKET for a keeper. Another installed home's
    declaration is an explicit exclusion. Conflicting and
    unknown declarations refuse; unknown ownership never passes silently.
    """
    if not isinstance(environment, dict):
        raise ReleaseRejectedError("launchd registration environment is not a dictionary")
    declared = cast("dict[str, object]", environment)
    declared_home = declared.get("AVA_HOME")
    scope = declared.get("AVA_JOB_SCOPE")
    keeper_socket = declared.get("AVA_PERMISSIONS_HELPER_SOCKET")
    if sum(value is not None for value in (declared_home, scope, keeper_socket)) > 1:
        raise ReleaseRejectedError("launchd registration has conflicting ownership declarations")
    if scope is not None:
        if scope != "machine":
            raise ReleaseRejectedError("launchd registration declares an unknown job scope")
        return "machine"
    if keeper_socket is not None:
        return _keeper_role(keeper_socket, home)
    if declared_home == str(home):
        return "unit"
    if _is_other_unit_home(declared_home, home):
        return "other-unit"
    raise ReleaseRejectedError("Ava launchd registration has unknown or other unit home")


def _launchd(
    home: Path,
) -> tuple[tuple[ExpectedLauncher, ...], tuple[ExcludedRegistration, ...]]:
    """Classify every com.ava.* registration in this user's LaunchAgents directory.

    Only a registration declaring this exact home is a unit launcher. Machine
    scope, this home's keeper, and positively identified other unit homes
    are recorded as explicit receipt exclusions; unknown declarations refuse.
    Every loaded com.ava.* label must resolve to a classified definition. A *.plist without a readable label is not a
    registration: it is skipped unless its filename claims the com.ava.*
    namespace, which refuses.
    """
    directory = Path.home() / "Library/LaunchAgents"
    if directory.resolve(strict=True) != directory:
        raise ReleaseRejectedError("launchd inventory directory is not canonical")
    result: list[ExpectedLauncher] = []
    excluded: list[ExcludedRegistration] = []
    for path in sorted(directory.glob("*.plist")):
        encoded = _regular_bytes(path)
        parsed: object = plistlib.loads(encoded)
        raw: dict[str, object] = (
            cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}
        )
        label = raw.get("Label")
        if not isinstance(label, str) or not label:
            # Not a launchd registration: a third-party file stays outside the
            # com.ava.* namespace, while a com.ava.*-named file still claims
            # Ava ownership and refuses instead of skipping silently.
            if path.name.startswith("com.ava."):
                raise ReleaseRejectedError("Ava launchd registration has no label")
            continue
        if not label.startswith("com.ava."):
            continue
        # A racing deletion reads as None here and refuses like any other change.
        if path.name != f"{label}.plist" or read_launchd_definition(label) != encoded:
            raise ReleaseRejectedError("launchd definition identity changed")
        digest = hashlib.sha256(encoded).hexdigest()
        role = _registration_role(raw.get("EnvironmentVariables", {}), home)
        if role == "unit":
            result.append(ExpectedLauncher(kind="launchd", name=label, definition_digest=digest))
        else:
            excluded.append(
                ExcludedRegistration(label=label, definition_digest=digest, classification=role)
            )
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    before = read_launchd_labels(deadline)
    after = read_launchd_labels(deadline)
    if before != after:
        raise ReleaseRejectedError("loaded launcher inventory changed")
    loaded = {label for label in after if label.startswith("com.ava.")}
    inventoried = {item.name for item in result} | {item.label for item in excluded}
    if not loaded <= inventoried:
        raise ReleaseRejectedError("loaded Ava job has no inventoried definition")
    return tuple(result), tuple(excluded)


def _launchers(
    home: Path,
) -> tuple[tuple[ExpectedLauncher, ...], tuple[ExcludedRegistration, ...]]:
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
    return tuple(result), ()


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


def _receipt_body(
    expected: ExpectedUnitWriters,
    excluded: tuple[ExcludedRegistration, ...],
    roster: list[dict[str, object]],
) -> dict[str, object]:
    """Assemble the sealed receipt body; PreparationReceipt is its consumer contract."""
    return {
        "version": 1,
        "expected": expected.model_dump(mode="json"),
        "services": roster,
        "excluded_registrations": [entry.model_dump(mode="json") for entry in excluded],
        "inventory_digest": expected.unit().inventory_digest,
        "closure": "unknown",
        "unresolved": [
            "non-session managed processes and predecessor orchestrator",
            "system-level or alternate-user relaunchers",
            "positive platform launcher shutdown observation",
        ],
    }


def collect_inventory(
    conn: psycopg.Connection,
    release: VerifiedRelease,
    home: Path,
    machine: str,
    *,
    schema_digest: str,
    allow_empty_launchers: bool = False,
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
    launchers, excluded = _launchers(home)
    if not launchers and not allow_empty_launchers:
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
        or (launchers, excluded) != _launchers(home)
        or roster != _service_roster()
    ):
        raise ReleaseRejectedError("unit inventory changed during preparation")
    return _receipt_body(expected, excluded, roster)


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


def revalidate_bootstrap_inventory(
    conn: psycopg.Connection,
    release: VerifiedRelease,
    home: Path,
    machine: str,
    path: Path,
    *,
    current_session: ExpectedSession,
    schema_digest: str,
) -> ExpectedUnitWriters:
    """Revalidate a retained receipt while allowing one proved A/B PID turnover.

    The restricted hop intentionally replaces the sole ``ava-ops`` process and
    quiesces its exact launcher, so byte-for-byte revalidation would reject every
    legitimate recovery. All other facts still come from the real producer and
    must remain exact. The changing session is supplied only after its native
    record, command, process identity, and verified A/B image have been checked;
    every remaining launcher must exactly match its prepared entry. The caller later
    compares the raw launcher table with the journaled original/quiesced bytes.
    """
    if path.parent != home / "run" or path.resolve(strict=True) != path:
        raise ReleaseRejectedError("inventory receipt is outside this unit")
    encoded = _regular_bytes(path)
    if path.name != f"release-inventory-{hashlib.sha256(encoded).hexdigest()}.json":
        raise ReleaseRejectedError("inventory receipt digest mismatch")
    try:
        prepared_raw: object = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ReleaseRejectedError("prepared unit inventory is malformed") from exc
    if not isinstance(prepared_raw, dict):
        raise ReleaseRejectedError("prepared unit inventory is malformed")
    prepared = cast("dict[str, object]", prepared_raw)
    try:
        prepared_inventory_digest = prepared["inventory_digest"]
        prepared_expected = ExpectedUnitWriters.model_validate_json(
            _canonical(prepared["expected"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseRejectedError("prepared unit inventory is malformed") from exc
    current = collect_inventory(
        conn,
        release,
        home,
        machine,
        schema_digest=schema_digest,
        allow_empty_launchers=True,
    )
    current_expected = ExpectedUnitWriters.model_validate_json(_canonical(current["expected"]))
    if (
        current_session.name != "ava-ops"
        or current_expected.sessions != (current_session,)
        or current_expected.processes != (current_session.process,)
        or any(item not in prepared_expected.launchers for item in current_expected.launchers)
        or current_expected.model_copy(update={"sessions": (), "processes": (), "launchers": ()})
        != prepared_expected.model_copy(update={"sessions": (), "processes": (), "launchers": ()})
    ):
        raise ReleaseRejectedError("prepared unit inventory observer substitution changed")
    normalized = dict(current)
    normalized["expected"] = prepared["expected"]
    normalized["inventory_digest"] = prepared_inventory_digest
    if _canonical(normalized) != encoded:
        raise ReleaseRejectedError("prepared unit inventory static facts changed")
    return prepared_expected


def assert_inventory_can_enter_maintenance(path: Path) -> None:
    """No consumer can reinterpret this bounded inventory as complete closure."""
    # This implementation has no complete non-session/OS-relauncher producer.
    # Adding one requires actual platform proof, not clearing a JSON flag.
    _regular_bytes(path)
    raise ReleaseRejectedError("managed writer coverage is unresolved; maintenance is forbidden")
