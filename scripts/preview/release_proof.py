"""Acquire, prepare and cycle two images in an initialized Linux four-service preview."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import signal
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from scripts.preview import local, preparation_process
from scripts.preview.release_cycle import CapturedReceipt, ReleaseCycle


def _request(path: Path, work: Path, gateway_port: int) -> bytes:
    captured = path.read_bytes()
    request = json.loads(captured)
    if (
        request["work"] != str(work / "acquisition")
        or re.fullmatch(r"[0-9a-f]{40}", request["commit"]) is None
        or request["frontend"]["gateway_port"] != gateway_port
    ):
        raise ValueError("image acquisition must bind exact commit, evidence work and preview port")
    return captured


class ReleaseProof:
    """One composition receipt; lifecycle effects and cleanup belong to ReleaseCycle."""

    preview: local.Preview
    work: Path
    path: Path
    requests: dict[str, bytes]
    proof: dict[str, Any]

    def __init__(self, preview: local.Preview, previous: Path, candidate: Path) -> None:
        if sys.platform != "linux":
            raise RuntimeError("release proof supports Linux four-service previews only")
        if (
            preview.data["state"] != "ready"
            or preview.data.get("verification") != "passed"
            or preview.data["cleanup"] != "pending"
            or set(preview.data["services"]) != {"gateway", "frontend", "ops", "agent-host"}
        ):
            raise RuntimeError("release proof requires a healthy initialized four-service preview")
        preview.assert_checkout()
        self.preview = preview
        self.work = preview.run / "release-proof"
        port = json.loads((preview.run / "config.json").read_text())["ports"]["gateway"]
        self.requests = {
            name: _request(path, self.work / name, port)
            for name, path in (("a", previous), ("b", candidate))
        }
        if (preview.run / "release-cycle-proof.json").exists():
            raise ValueError("release cycle evidence already exists; use a fresh preview")
        self.work.mkdir(mode=0o700)
        self.path = self.work / "proof.json"
        self.proof = {
            "version": 1,
            "scope": "Linux four-service completed-work same-schema A/B/A with scripted model",
            "source_commit": preview.data["commit"],
            "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "requests": {},
            "phases": {},
            "result": "running",
            "cleanup": "source preview retained until cycle takes custody",
        }
        for name, captured in self.requests.items():
            (self.work / name).mkdir(mode=0o700)
            path = self.work / name / "acquisition-request.json"
            path.write_bytes(captured)
            self.proof["requests"][name] = {
                "path": str(path),
                "sha256": hashlib.sha256(captured).hexdigest(),
                "commit": json.loads(captured)["commit"],
            }
        self.save()

    def save(self) -> None:
        local.write_json(self.path, self.proof)

    @contextmanager
    def phase(self, name: str) -> Generator[None]:
        record: dict[str, Any] = {"result": "running", "started_at": time.time()}
        self.proof["phases"][name] = record
        self.save()
        try:
            yield
            record["result"] = "passed"
        except BaseException as exc:
            record.update(result="failed", error=repr(exc))
            raise
        finally:
            record["finished_at"] = time.time()
            self.save()

    def prepare(self, name: str) -> CapturedReceipt:
        work = self.work / name
        # Fresh source interpreter and OS allowlist; neither acquisition nor this
        # controller imports target Settings or forwards provider/Python authority.
        preparation_process.run(
            [
                str(self.preview.source / ".venv/bin/python"),
                "-m",
                "scripts.preview.release_inputs",
                "--request",
                str(work / "acquisition-request.json"),
                "--work",
                str(work),
                "--store",
                str(self.preview.home / "releases"),
            ],
            cwd=self.preview.source,
            env=local.clean_env(),
            log=work / "preparation.log",
            evidence=work / "custody.json",
            timeout=7200,
        )
        prepared = json.loads((work / "prepared.json").read_text())
        receipt = CapturedReceipt.read(work / "preparation/receipt.json")
        if (
            prepared["request_digest"] != self.proof["requests"][name]["sha256"]
            or prepared["preparation"] != str(receipt.path)
            or prepared["preparation_digest"] != receipt.digest
            or receipt.commit != self.proof["requests"][name]["commit"]
        ):
            raise RuntimeError("image preparation evidence differs from captured proof inputs")
        (work / "captured-receipt.json").write_bytes(receipt.encoded)
        self.proof["requests"][name].update(
            preparation=str(receipt.path), preparation_digest=receipt.digest
        )
        self.save()
        return receipt

    def cycle(self, previous: CapturedReceipt, candidate: CapturedReceipt) -> None:
        cycle = ReleaseCycle(self.preview, previous, candidate)
        self.proof["cleanup"] = "owned by release-cycle-proof.json"
        self.save()
        cycle.run()
        if cycle.proof["result"] != "passed":
            raise RuntimeError("release cycle did not verify all phases and cleanup")

    def run(self) -> None:
        try:
            with self.phase("prepare-a"):
                previous = self.prepare("a")
            with self.phase("prepare-b"):
                candidate = self.prepare("b")
            with self.phase("release-cycle"):
                self.cycle(previous, candidate)
            self.proof["result"] = "passed"
        except BaseException as exc:
            self.proof.update(result="failed", error=repr(exc))
            raise
        finally:
            self.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--previous", type=Path, required=True, help="Exact A Acquisition JSON")
    parser.add_argument("--candidate", type=Path, required=True, help="Exact B Acquisition JSON")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, local.interrupted)
    preview = local.Preview(args.run)
    # ReleaseCycle.run does not acquire a lock: its CLI owns that boundary too.
    with (preview.run / "operation.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ReleaseProof(preview, args.previous, args.candidate).run()


if __name__ == "__main__":
    main()
