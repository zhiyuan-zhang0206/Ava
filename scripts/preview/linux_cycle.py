"""Prove ordinary and systemd lifecycle on one disposable Linux branch preview.

Run from a checkout with ``python3 -m scripts.preview.linux_cycle --ref BRANCH``.
This controller owns no services: every effect uses the candidate's ordinary
CLI or its native manager. A failure is retained even when cleanup succeeds.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import shutil
import signal
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from scripts.preview import local
from shared.native_process import process_birth_key

TERMINAL = "ava-preview-lifetime"


def preserved(first: dict[str, Any], second: dict[str, Any]) -> None:
    """Identity/configuration must survive every transition before destroy."""
    for field in ("ports", "hashes"):
        if first[field] != second[field]:
            raise RuntimeError(f"Lifecycle changed {field}: {first[field]} -> {second[field]}")
    if (
        "service_path" in first
        and "service_path" in second
        and first["service_path"] != second["service_path"]
    ):
        raise RuntimeError("Manager and interactive service environments differ")


def births(first: dict[str, Any], second: dict[str, Any], field: str, *, same: bool) -> None:
    """Compare exact native births, never just PID existence or a success reply."""
    before, after = first[field], second[field]
    if set(before) != set(after):
        raise RuntimeError(f"Lifecycle changed the {field} roster")
    wrong = [
        name
        for name in before
        if (
            process_birth_key(**before[name], platform="linux")
            == process_birth_key(**after[name], platform="linux")
        )
        != same
    ]
    if wrong:
        expectation = "retained" if same else "replaced"
        raise RuntimeError(f"Expected {expectation} {field}: {wrong}")


def retained_terminal(first: dict[str, Any], second: dict[str, Any]) -> None:
    for observation in (first, second):
        if TERMINAL not in observation["terminals"]:
            raise RuntimeError("The deliberately persistent terminal disappeared")
    before, after = first["terminals"][TERMINAL], second["terminals"][TERMINAL]
    for field in ("host", "shell", "generation", "record_sha256"):
        if before[field] != after[field]:
            raise RuntimeError(f"Application replacement changed terminal {field}")


class LinuxCycle:
    def __init__(self, preview: local.Preview):
        self.preview = preview
        self.python = str(preview.source / ".venv/bin/python")
        self.path = preview.run / "cycle-proof.json"
        if self.path.exists():
            raise ValueError("Cycle evidence already exists; create a fresh preview")
        self.proof: dict[str, Any] = {
            "source_commit": preview.data["commit"],
            "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "scope": "Linux ordinary and systemd lifecycle, private data plane, scripted model with real execution",
            "excludes": preview.data["excludes"],
            "phases": {},
            "result": "running",
        }
        local.write_json(self.path, self.proof)

    @contextmanager
    def phase(self, name: str) -> Generator[None]:
        record: dict[str, Any] = {"result": "running", "started_at": time.time()}
        self.proof["phases"][name] = record
        local.write_json(self.path, self.proof)
        try:
            yield
            record["result"] = "passed"
        except BaseException as exc:
            record.update(result="failed", error=repr(exc))
            raise
        finally:
            record["finished_at"] = time.time()
            local.write_json(self.path, self.proof)

    def observe(self, label: str, mode: str) -> dict[str, Any]:
        self.preview.command(
            f"observe-{label}",
            [
                self.python,
                "-m",
                "scripts.preview.linux_observer",
                str(self.preview.run),
                label,
                mode,
            ],
        )
        observed = json.loads((self.preview.run / f"cycle-{label}.json").read_text())
        if observed["source_commit"] != self.preview.data["commit"]:
            raise RuntimeError("Observed a different source revision")
        return observed

    def smoke(self, label: str) -> None:
        # No delay/retry between declared readiness and admission of new work.
        self.preview.runtime("smoke")
        self.preview.runtime("check")
        shutil.copyfile(self.preview.run / "smoke.json", self.preview.run / f"smoke-{label}.json")

    def ordinary(self) -> dict[str, Any]:
        preview = self.preview
        shutil.copyfile(preview.run / "smoke.json", preview.run / "smoke-initial.json")
        initial = self.observe("initial", "running")
        preview.cli("bare-repeat", ["start"])
        repeated = self.observe("repeat", "running")
        preserved(initial, repeated)
        for field in ("births", "data_births"):
            births(initial, repeated, field, same=True)
        preview.cli("ordinary-stop", ["stop", "-y", "--stop-browser"])
        preserved(initial, self.observe("ordinary-stopped", "stopped"))
        preview.cli("bare-resume", ["start"])
        self.smoke("resumed")
        resumed = self.observe("resumed", "running")
        preserved(initial, resumed)
        for field in ("births", "data_births"):
            births(initial, resumed, field, same=False)
        preview.cli("before-manager-stop", ["stop", "-y", "--stop-browser"])
        preserved(initial, self.observe("before-manager", "stopped"))
        return initial

    def manager(self, initial: dict[str, Any]) -> None:
        preview = self.preview
        preview.command(
            "manager-install",
            [self.python, "-c", "from shared.os_boot_unit import install; install()"],
        )
        preview.command(
            "manager-name",
            [
                self.python,
                "-c",
                "from shared.os_boot_unit import unit_name; from shared.paths import ava_home; print(unit_name(ava_home()))",
            ],
        )
        unit = Path(preview.data["steps"][-1]["log"]).read_text().strip().splitlines()[-1]
        if not unit.startswith("ava-boot.home-") or not unit.endswith(".service"):
            raise RuntimeError(f"Unexpected preview manager unit: {unit}")
        preview.command("manager-start", ["sudo", "-n", "systemctl", "start", unit], timeout=1000)
        self.smoke("manager")
        preview.command(
            "persistent-terminal",
            [
                self.python,
                "-c",
                "import os\n"
                "from pathlib import Path\n"
                "from shared.session_backend import get_shell_backend\n"
                f"if not get_shell_backend().new_session({TERMINAL!r}, '', Path.cwd(), env=dict(os.environ)):\n"
                "    raise RuntimeError('Could not create the native terminal fixture')\n",
            ],
        )
        managed = self.observe("manager-initial", "manager-running")
        preserved(initial, managed)
        preview.cli("manager-bare-repeat", ["start"])
        repeated = self.observe("manager-repeat", "manager-running")
        preserved(managed, repeated)
        retained_terminal(managed, repeated)
        for field in ("births", "data_births"):
            births(managed, repeated, field, same=True)
        preview.command("manager-stop", ["sudo", "-n", "systemctl", "stop", unit], timeout=120)
        retained = self.observe("manager-stopped", "manager-stopped")
        preserved(initial, retained)
        births(managed, retained, "data_births", same=True)
        retained_terminal(managed, retained)
        preview.command("manager-resume", ["sudo", "-n", "systemctl", "start", unit], timeout=1000)
        self.smoke("manager-resumed")
        resumed = self.observe("manager-resumed", "manager-running")
        preserved(initial, resumed)
        births(retained, resumed, "data_births", same=True)
        births(managed, resumed, "births", same=False)
        retained_terminal(managed, resumed)

    def cleanup(self) -> None:
        python = self.preview.source / ".venv/bin/python"
        config = self.preview.run / "config.json"
        observed_initial = self.proof["phases"]["initial"]["result"] == "passed"
        missing = observed_initial and (not python.exists() or not config.exists())
        message = "Candidate interpreter or port evidence disappeared before cleanup"
        try:
            if python.exists():
                if self.preview.data["cleanup"] != "passed":
                    self.preview.stop()
                if config.exists():
                    self.observe("destroyed", "destroyed")
        except BaseException as exc:
            if missing:
                exc.add_note(message)
            raise
        if missing:
            raise RuntimeError(message)

    def run(self) -> None:
        try:
            with self.phase("initial"):
                local.run_preview(self.preview, keep=True)
            with self.phase("ordinary"):
                initial = self.ordinary()
            with self.phase("manager"):
                self.manager(initial)
        finally:
            try:
                with self.phase("cleanup"):
                    self.cleanup()
            finally:
                phases = self.proof["phases"]
                complete = set(phases) == {"initial", "ordinary", "manager", "cleanup"}
                passed = complete and all(p["result"] == "passed" for p in phases.values())
                self.proof["result"] = "passed" if passed else "failed"
                local.write_json(self.path, self.proof)


def main() -> None:
    if sys.platform != "linux":
        raise SystemExit("Run this scenario inside Linux, for example an isolated OrbStack machine")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--root", type=Path, default=Path.home() / ".ava-previews")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, local.interrupted)
    preview = local.create(args.repo, args.ref, args.root)
    with (preview.run / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        LinuxCycle(preview).run()


if __name__ == "__main__":
    main()
