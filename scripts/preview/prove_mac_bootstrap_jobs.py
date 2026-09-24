"""Operator-invoked native definition/custody proof in an installed preview home.

Run with the preview AVA_HOME explicitly selected. This creates two uniquely
named, unloaded /usr/bin/true LaunchAgents, kills its own child after an atomic
custody move, and recovers from private serialized originals. It never loads or
signals a job. This is native primitive evidence, not complete A/B image proof.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cli.commands._update_bootstrap import native
from shared.cluster.registry import load_registry
from shared.config import settings
from shared.machine import machine_name
from shared.managed_writer_observation import ExpectedLauncher, ExpectedProcess, ExpectedUnitWriters
from shared.private_storage import write_private_bytes
from shared.updater_recovery import LaunchdRecovery


def _require(condition: bool, message: str) -> None:  # noqa: FBT001 -- assertion predicate.
    if not condition:
        raise RuntimeError(message)


def _read(directory: Path) -> tuple[ExpectedUnitWriters, tuple[LaunchdRecovery, ...]]:
    raw = json.loads((directory / "originals.json").read_bytes())
    expected = ExpectedUnitWriters.model_validate_json(json.dumps(raw["expected"]))
    snapshots = tuple(
        LaunchdRecovery.model_validate_json(json.dumps(item)) for item in raw["launchd"]
    )
    return expected, snapshots


def _worker(directory: Path) -> None:
    _expected, snapshots = _read(directory)
    calls = 0

    def authorized() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            os._exit(91)  # Intentional real process death after the first durable move.

    native.quiesce_launchd(snapshots, datetime.now(UTC) + timedelta(seconds=30), authorized)
    raise AssertionError("fault was not injected")


def _prepare(home: Path, nonce: str, until: datetime) -> ExpectedUnitWriters:
    launchers: list[ExpectedLauncher] = []
    for suffix in ("a", "b"):
        label = f"com.ava.mac-native-proof.{nonce}.{suffix}"
        path = native._path(label)
        _require(
            not path.exists() and not native._loaded(label, until), "proof label already exists"
        )
        body = plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": ["/usr/bin/true"],
                "EnvironmentVariables": {"AVA_HOME": str(home)},
                "RunAtLoad": True,
            }
        )
        # Exclusive publication: the proof never overwrites an existing job.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        import hashlib

        launchers.append(
            ExpectedLauncher(
                kind="launchd",
                name=label,
                definition_digest=hashlib.sha256(body).hexdigest(),
            )
        )
    return ExpectedUnitWriters(
        machine=machine_name(),
        home=str(home),
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        processes=(ExpectedProcess(pid=os.getpid(), create_time=1.0, starttime=None),),
        sessions=(),
        launchers=tuple(launchers),
    )


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--fault-worker":
        _worker(Path(sys.argv[2]))
        return
    home = settings.general.ava_home
    _require(sys.platform == "darwin", "native proof requires Mac")
    _require(
        home.is_absolute() and home.resolve(strict=True) == home, "preview home must be canonical"
    )
    _require(not home.samefile(Path.home() / ".ava"), "native proof refuses the production home")
    _require(str(home) in load_registry(), "native proof needs an installed preview cluster")
    nonce = uuid4().hex
    directory = home / "run" / f"mac-native-proof-{nonce}"
    until = datetime.now(UTC) + timedelta(minutes=2)
    snapshots: tuple[LaunchdRecovery, ...] = ()
    labels = [f"com.ava.mac-native-proof.{nonce}.{suffix}" for suffix in ("a", "b")]
    try:
        expected = _prepare(home, nonce, until)
        snapshots = native.capture_launchd(expected, ["/usr/bin/true"], until)
        write_private_bytes(
            directory / "originals.json",
            json.dumps(
                {
                    "expected": expected.model_dump(mode="json"),
                    "launchd": [item.model_dump(mode="json") for item in snapshots],
                },
                sort_keys=True,
            ).encode(),
        )
        child = subprocess.run(  # noqa: S603 -- this exact proof module and private nonce directory.
            [
                sys.executable,
                "-m",
                "scripts.preview.prove_mac_bootstrap_jobs",
                "--fault-worker",
                str(directory),
            ],
            check=False,
            timeout=40,
            capture_output=True,
        )
        _require(child.returncode == 91, "fault worker did not die at the recorded boundary")
        _require(not native._path(labels[0]).exists(), "first definition was not moved")
        _require(native._path(labels[1]).exists(), "second definition was unexpectedly changed")
        expected, retained = _read(directory)
        recovered = native.capture_launchd(expected, ["/usr/bin/true"], until, retained=retained)
        native.restore_launchd(recovered, until, lambda: None)
        _require(
            all(native._definition_state(item) is not None for item in recovered),
            "restore is incomplete",
        )
        _require(
            all(not native._loaded(item.label, until) for item in recovered), "restore loaded a job"
        )
        native.quiesce_launchd(recovered, until, lambda: None)
        native.verify_quiesced(recovered, until)
        native.restore_launchd(recovered, until, lambda: None)
        write_private_bytes(
            directory / "result.json",
            json.dumps(
                {
                    "result": "pass",
                    "fault_exit": child.returncode,
                    "cases": [
                        "partial-move-process-crash",
                        "restore-exact-originals",
                        "repeat-quiesce-restore",
                    ],
                    "loaded_jobs": 0,
                    "full_image_hop_proven": False,
                },
                sort_keys=True,
            ).encode(),
        )
        print(directory / "result.json")
    finally:
        if snapshots:
            # Use the same custody operation for cleanup; public definitions
            # are never removed after a pathname-only check.
            native.quiesce_launchd(snapshots, until, lambda: None)
            for item in snapshots:
                _require(native._custody_state(item) is not None, "cleanup custody disappeared")
                Path(item.custody).unlink()


if __name__ == "__main__":
    main()
