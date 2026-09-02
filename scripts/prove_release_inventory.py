"""CI-only source-absent preparation to real process/session observer proof.

OS scheduler reads use bounded fixture responses; no job is installed or changed.
The actual producer parses real private files and native PG unit registration.
The observer must report unknown closure, never a synthetic positive barrier.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import plistlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psutil
import psycopg

from cli.commands import _release_inventory as inventory
from shared.managed_writer_observation import (
    ExpectedUnitWriters,
    ObservationChallenge,
    UnitObserver,
)
from shared.runtime_prepare import tree_inventory
from shared.runtime_release import ReleaseRejectedError, verify_release
from shared.session_record import SessionRecord, pid_starttime_ticks


def require(value: bool, message: str) -> None:  # noqa: FBT001 — CI predicate.
    if not value:
        raise AssertionError(message)


def check_observer(expected: ExpectedUnitWriters, receipt: Path) -> None:
    require(
        {item.name for item in expected.sessions}
        == {"ava-restarter", "ava-obsolete-plugin-service"},
        "residual session omitted",
    )
    payload = json.loads(receipt.read_bytes())
    require(
        any(row["session"] == "runtime-fixture" for row in payload["services"]),
        "plugin service omitted",
    )
    challenge = ObservationChallenge(
        challenge=uuid4(), valid_until=datetime.now(UTC) + timedelta(minutes=1)
    )
    response = asyncio.run(
        UnitObserver(expected, challenge).respond(
            json.dumps({"challenge": str(challenge.challenge)}).encode()
        )
    )
    require(response[0] == 200, "actual observer refused inventory")
    observation = json.loads(response[1])
    require(observation["closure"] == "unknown", "unknown closure became permission")
    require(
        observation["sessions"] == ["record_present", "record_present"],
        "session observation differed",
    )
    require(observation["processes"] == ["alive"], "actual process was not observed")
    try:
        inventory.assert_inventory_can_enter_maintenance(receipt)
    except ReleaseRejectedError:
        pass
    else:
        raise AssertionError("incomplete coverage entered maintenance")


def session_fixture(home: Path) -> SessionRecord:
    check_bounded_read(home)
    sessions = home / "run/sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    process = psutil.Process()
    record = SessionRecord(
        pid=process.pid,
        create_time=process.create_time(),
        cmd="CI retained process",
        cwd=str(home),
        started_at=process.create_time(),
        starttime=pid_starttime_ticks(process.pid),
    )
    record.write(sessions / "ava-restarter.json")
    record.write(sessions / "ava-obsolete-plugin-service.json")
    return record


def check_bounded_read(home: Path) -> None:
    path = home / "inventory-read-fixture"
    path.write_bytes(b"original")
    require(inventory._regular_bytes(path) == b"original", "regular inventory read failed")
    replacement = home / "inventory-read-replacement"
    replacement.write_bytes(b"replacement")
    original_open = os.open

    def replace_before_open(target: Path, flags: int) -> int:
        replacement.replace(target)
        return original_open(target, flags)

    with patch.object(inventory.os, "open", side_effect=replace_before_open):
        try:
            inventory._regular_bytes(path)
        except ReleaseRejectedError:
            pass
        else:
            raise AssertionError("replaced inventory inode accepted")
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    try:
        inventory._regular_bytes(path)
    except ReleaseRejectedError:
        pass
    else:
        raise AssertionError("oversized inventory accepted")
    path.unlink()


def main() -> None:
    if os.environ["GITHUB_ACTIONS"] != "true":
        raise RuntimeError("CI-only inventory proof")
    home = Path(os.environ["AVA_HOME"]).resolve()
    if not home.is_relative_to(Path(os.environ["RUNNER_TEMP"]).resolve()):
        raise RuntimeError("inventory proof requires runner scratch home")
    artifact, manifest, schema = sys.argv[1:]
    release = verify_release(
        home / "releases",
        artifact,
        manifest_digest=manifest,
        platform_tag=platform.platform(),
        schema_digest=schema,
    )
    original_image = tree_inventory(release.root)
    sessions = home / "run/sessions"
    record = session_fixture(home)
    launch_agents = Path.home() / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    label = "com.ava.runtime-proof.legacy-relauncher"
    job = launch_agents / f"{label}.plist"
    job.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "EnvironmentVariables": {"AVA_HOME": str(home)},
                "ProgramArguments": ["/obsolete/source/.venv/bin/ava", "start"],
            }
        )
    )
    original_job = job.read_bytes()
    cron = f"@reboot AVA_HOME={home} /obsolete/ava start # ava-runtime-proof\n"

    def scheduler_read(argv: list[str]) -> str:
        if argv == ["/bin/launchctl", "list"]:
            return f"-\t0\t{label}\n"
        if argv == ["/usr/bin/crontab", "-l"]:
            return cron
        raise AssertionError("unexpected scheduler read")

    with (
        psycopg.connect(os.environ["AVA_DB_URL"]) as conn,
        patch.object(inventory, "_read_command", side_effect=scheduler_read),
    ):
        conn.execute("CREATE TEMP TABLE machine_units(machine_name text,home text)")
        conn.execute("INSERT INTO machine_units VALUES (%s,%s)", ("runtime-proof", str(home)))
        receipt = inventory.prepare_unit_inventory(
            conn,
            release,
            home,
            "runtime-proof",
            schema_digest=schema,
        )
        expected = inventory.revalidate_prepared_inventory(
            conn,
            release,
            home,
            "runtime-proof",
            receipt,
            schema_digest=schema,
        )
        check_observer(expected, receipt)
        # Omission of a residual old session, service-only roster drift, and
        # unit relocation must all invalidate the full prepared receipt.
        original_roster = inventory._service_roster
        for change in ("session", "launcher", "service", "unit"):
            with patch.object(inventory, "_service_roster", wraps=original_roster) as roster:
                if change == "session":
                    (sessions / "ava-obsolete-plugin-service.json").unlink()
                elif change == "launcher":
                    cron += "@reboot AVA_HOME=" + str(home) + " /old/ava start # ava-extra\n"
                    job.write_bytes(plistlib.dumps({"Label": label}))
                elif change == "service":
                    roster.side_effect = lambda: [
                        row for row in original_roster() if row["session"] != "runtime-fixture"
                    ]
                else:
                    conn.execute("UPDATE machine_units SET home='/changed'")
                try:
                    inventory.revalidate_prepared_inventory(
                        conn, release, home, "runtime-proof", receipt, schema_digest=schema
                    )
                except ReleaseRejectedError:
                    pass
                else:
                    raise AssertionError(f"changed {change} inventory accepted")
                record.write(sessions / "ava-obsolete-plugin-service.json")
                job.write_bytes(original_job)
                cron = f"@reboot AVA_HOME={home} /obsolete/ava start # ava-runtime-proof\n"
        conn.rollback()
    require(tree_inventory(release.root) == original_image, "proof wrote into sealed image")
    (home.parent / "inventory-proof.json").write_text(
        json.dumps(
            {
                "actualPreparedReceipt": True,
                "actualObserverConsumer": True,
                "residualSessionsIncluded": True,
                "pluginServiceIncluded": True,
                "changedFactsRefused": True,
                "unknownCoverageBlocksMaintenance": True,
                "sealedImageUnchanged": True,
                "schedulerSource": "controlled read-only fixture",
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
