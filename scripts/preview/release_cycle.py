"""Completed-work A/B/A release proof on one initialized disposable Linux preview.

The source preview and both complete images must already exist. No acquisition,
source mutation, service launcher, or failed-transition retry lives here.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import shutil
import signal
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.preview import local
from scripts.preview.linux_cycle import births


@dataclass(frozen=True)
class CapturedReceipt:
    path: Path
    encoded: bytes
    commit: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.encoded).hexdigest()

    @classmethod
    def read(cls, path: Path) -> CapturedReceipt:
        path = path.resolve(strict=True)
        with path.open("rb") as stream:
            encoded = stream.read(2 * 1024 * 1024 + 1)
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("preparation receipt exceeds the proof read budget")
        value = json.loads(encoded)
        commit = value["source"]["source_commit"]
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None or value["request"]["commit"] != commit:
            raise ValueError("preparation receipt has inconsistent committed source")
        return cls(path, encoded, commit)

    def require_unchanged(self) -> None:
        if self.read(self.path) != self:
            raise RuntimeError("captured preparation receipt changed before effect preflight")


class ReleaseCycle:
    def __init__(
        self, preview: local.Preview, previous: CapturedReceipt, candidate: CapturedReceipt
    ) -> None:
        self.preview = preview
        self.receipts = (previous, candidate)
        for receipt in self.receipts:
            receipt.require_unchanged()
        self.previous, self.candidate = previous.path, candidate.path
        self.python = str(preview.source / ".venv/bin/python")
        self.path = preview.run / "release-cycle-proof.json"
        if self.path.exists():
            raise ValueError("Release-cycle evidence already exists; use a fresh preview")
        self.proof: dict[str, Any] = {
            "source_commit": preview.data["commit"],
            "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "preparation_receipts": {"a": str(self.previous), "b": str(self.candidate)},
            "receipt_bindings": {
                name: {"sha256": receipt.digest, "commit": receipt.commit}
                for name, receipt in zip(("a", "b"), self.receipts, strict=True)
            },
            "scope": "Linux four-service completed-work same-schema A/B/A, scripted model with real execution",
            "excludes": [
                "in-flight recovery",
                "schema changes",
                "fleet",
                "real providers",
                "production",
            ],
            "phases": {},
            "effects_started": False,
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

    def command(
        self, name: str, argv: list[str], *, cwd: Path | None = None, timeout: float = 1000
    ) -> None:
        # Every subprocess starts with a new allowlisted environment, never a
        # retained Settings instance or inherited provider/Python overrides.
        self.preview.env = local.clean_env() | {
            "AVA_HOME": str(self.preview.home),
            "AVA_CLUSTER_REGISTRY": str(self.preview.run / "clusters.json"),
        }
        self.preview.command(name, argv, cwd=cwd, timeout=timeout)

    def adapter(self, action: str, *args: str) -> None:
        self.command(
            f"release-{action}",
            [
                self.python,
                "-m",
                "scripts.preview.release_cycle_runtime",
                str(self.preview.run),
                action,
                *args,
            ],
        )

    def cli(self, name: str, args: list[str]) -> None:
        self.command(name, [self.python, "-m", "cli.main", *args])

    def observe(
        self, label: str, *, receipt: Path | None = None, destroyed: bool = False
    ) -> dict[str, Any]:
        mode = "destroyed" if destroyed else "manager-running" if receipt else "running"
        argv = [
            self.python,
            "-m",
            "scripts.preview.linux_observer",
            str(self.preview.run),
            label,
            mode,
        ]
        if receipt is not None:
            argv.extend(("--runtime-receipt", str(receipt)))
        self.command(f"observe-{label}", argv)
        observed = json.loads((self.preview.run / f"cycle-{label}.json").read_text())
        if (
            observed["result"] != "passed"
            or observed["source_commit"] != self.preview.data["commit"]
        ):
            raise RuntimeError("observer did not verify the captured source context")
        return observed

    def smoke(self, label: str) -> None:
        # The first post-readiness action admits business work; no observer wait.
        for action in ("smoke", "check"):
            self.command(
                f"release-{label}-{action}",
                [self.python, str(self.preview.run / "runtime.py"), str(self.preview.run), action],
            )
        shutil.copyfile(
            self.preview.run / "smoke.json", self.preview.run / f"smoke-release-{label}.json"
        )

    def initialized(self) -> dict[str, Any]:
        preview = self.preview
        if sys.platform != "linux":
            raise RuntimeError("release cycle is Linux-only")
        if (
            preview.data["state"] != "ready"
            or preview.data["cleanup"] == "passed"
            or preview.data.get("verification") != "passed"
        ):
            raise RuntimeError("release cycle requires an initialized, running source preview")
        if set(preview.data["services"]) != {"gateway", "frontend", "ops", "agent-host"}:
            raise RuntimeError("release cycle requires the four-service core profile")
        preview.assert_checkout()
        shutil.copyfile(preview.run / "smoke.json", preview.run / "smoke-release-source.json")
        self.adapter(
            "prepare",
            "--previous",
            str(self.previous),
            "--previous-digest",
            self.receipts[0].digest,
            "--previous-commit",
            self.receipts[0].commit,
            "--candidate",
            str(self.candidate),
            "--candidate-digest",
            self.receipts[1].digest,
            "--candidate-commit",
            self.receipts[1].commit,
        )
        self.proof["effects_started"] = True
        local.write_json(self.path, self.proof)
        self.adapter("freeze", "--label", "source")
        observed = self.observe("release-source")
        self.adapter("capture", "--label", "source")
        return observed

    @staticmethod
    def preserved(first: dict[str, Any], second: dict[str, Any]) -> None:
        # PATH's runtime prefix must change with the image. The observer checks
        # that exact new prefix; immutable home/configuration hashes cannot change.
        for field in ("ports", "hashes"):
            if first[field] != second[field]:
                raise RuntimeError(f"release transition changed {field}")
        if first["service_path"]["declared"] != second["service_path"]["declared"]:
            raise RuntimeError("release transition changed the admitted host PATH")
        births(first, second, "data_births", same=True)
        births(first, second, "births", same=False)

    def image_a(self, initial: dict[str, Any]) -> dict[str, Any]:
        self.cli("release-source-stop", ["stop", "-y", "--stop-browser", "--keep-infra"])
        self.adapter("closed", "--label", "source")
        self.adapter("initial")
        inputs = json.loads((self.preview.run / "release-inputs.json").read_text())
        self.command(
            "release-initial-manager-start",
            ["sudo", "-n", "/usr/bin/systemctl", "start", inputs["unit"]],
        )
        self.smoke("a")
        observed = self.observe("release-a", receipt=self.previous)
        self.preserved(initial, observed)
        self.adapter("state", "--label", "a")
        self.adapter("freeze", "--label", "a")
        self.adapter("capture", "--label", "a")
        return observed

    def transition(self, label: str, target: str, previous: dict[str, Any]) -> dict[str, Any]:
        self.adapter("submit", "--label", label)
        self.adapter("wait", "--label", label)
        self.adapter("closed", "--label", "a" if label == "ab" else "b")
        smoke_label = "b" if target == "b" else "a-return"
        self.smoke(smoke_label)
        self.adapter("submit", "--label", label)  # Completed-only public retirement.
        self.adapter("retired", "--label", label)
        observed = self.observe(
            f"release-{smoke_label}", receipt=self.candidate if target == "b" else self.previous
        )
        self.preserved(previous, observed)
        self.adapter("state", "--label", smoke_label)
        self.adapter("freeze", "--label", smoke_label)
        self.adapter("capture", "--label", smoke_label)
        return observed

    def cleanup(self) -> None:
        if not self.proof["effects_started"]:
            return
        # A finite executor is a live mutation authority. Never destroy its home
        # until its exact native job/cgroup is closed; no controller retry here.
        self.adapter("settle")
        try:
            self.cli("release-stop", ["stop", "-y", "--stop-browser"])
            self.cli("release-destroy", ["cluster", "destroy", "--path", str(self.preview.home)])
            self.command(
                "release-verify-stopped",
                [
                    self.python,
                    str(self.preview.run / "runtime.py"),
                    str(self.preview.run),
                    "verify-stopped",
                ],
            )
        finally:
            self.observe("release-destroyed", destroyed=True)
        self.preview.data.update(cleanup="passed", state="stopped")
        self.preview.save()

    def run(self) -> None:
        try:
            with self.phase("inputs"):
                initial = self.initialized()
            with self.phase("image-a"):
                a = self.image_a(initial)
            with self.phase("a-to-b"):
                b = self.transition("ab", "b", a)
            with self.phase("b-to-a"):
                self.transition("ba", "a", b)
        finally:
            try:
                with self.phase("cleanup"):
                    self.cleanup()
            finally:
                phases = self.proof["phases"]
                complete = set(phases) == {"inputs", "image-a", "a-to-b", "b-to-a", "cleanup"}
                self.proof["result"] = (
                    "passed"
                    if complete and all(row["result"] == "passed" for row in phases.values())
                    else "failed"
                )
                local.write_json(self.path, self.proof)


def main() -> None:
    if sys.platform != "linux":
        raise SystemExit("Run this completed-work image cycle inside disposable Linux")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, local.interrupted)
    preview = local.Preview(args.run)
    with (preview.run / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ReleaseCycle(
            preview, CapturedReceipt.read(args.previous), CapturedReceipt.read(args.candidate)
        ).run()


if __name__ == "__main__":
    main()
