"""Explicit executable expectations for a source-checkout Linux preview observation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from base.sessions.env_forwarding import frontend_toolchain_path, normalize_service_path


@dataclass(frozen=True)
class ExpectedRuntime:
    interpreter: Path
    cwd: Path

    def argv(self, run: Path) -> list[str]:
        directory = run / "home/run/ava-root"
        return [
            str(self.interpreter),
            "-m",
            "services.ava_root",
            "--run-dir",
            str(directory),
            "--manifests",
            str(directory / "manifests.json"),
            "--wiring",
            "services.ava_root_glue.glue:build_wiring",
        ]

    def environment(self, run: Path, declared: str) -> dict[str, str]:
        bindir = self.interpreter.parent
        host_path = normalize_service_path(declared, excluded=(bindir,))
        return {
            "AVA_HOME": str(run / "home"),
            "AVA_HOST_STATE_DIR": str(run),
            "VIRTUAL_ENV": str(bindir.parent),
            "AVA_SERVICE_PATH": host_path,
            "PATH": normalize_service_path(
                ":".join((str(bindir), host_path, frontend_toolchain_path("")))
            ),
        }


def expected_runtime(run: Path) -> ExpectedRuntime:
    """The preview's own checkout and its virtualenv interpreter."""
    source = run / "source"
    return ExpectedRuntime(source / ".venv/bin/python", source)


def environment_digest(environment: dict[str, str]) -> str:
    """Report complete native environment comparison evidence without secrets."""
    return hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
