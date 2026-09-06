"""CI-only wheel closure proof; no cluster startup or production configuration.

Run with an isolated interpreter after installing the wheel non-editably.
The source checkout must not be on sys.path. The negative control removes the
exec-child member from a wheel and must fail before any runtime import.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

PACKAGES = ("ava", "cli", "agent", "shared", "ops", "gateway", "services", "ava_builtins")
REQUIRED = (
    "agent/exec_child.py",
    "agent/graph/_exec_subprocess.py",
    "services/agent_host/daemon.py",
    "services/restarter/daemon.py",
    "gateway/app.py",
    "ops/spec.py",
    "shared/dotenv_boot.py",
    "cli/main.py",
    "cli/python_install.py",
    "shared/python_lock.py",
    "ava_builtins/plugins/ava_code/plugin.py",
    "ava_builtins/plugins/ava_code/ava_code.ava.okf.md",
    "db/schema.sql",
    "commands/compact.md",
    "schedules/manifest.json",
    "deploy/otel-collector/otel-collector.yaml",
)


def verify_members(wheel: Path) -> None:
    """Reject SDK-only wheels and any wheel carrying editable bootstrap files."""
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    missing = sorted(set(REQUIRED) - names)
    if missing:
        raise ValueError(f"runtime wheel missing members: {missing}")
    for package in PACKAGES:
        if not any(name.startswith(f"{package}/") for name in names):
            raise ValueError(f"runtime wheel missing package: {package}")
    if any(name.endswith(".pth") for name in names):
        raise ValueError("runtime wheel contains an editable/path injection file")
    migrations = {
        name for name in names if name.startswith("migrations/") and name.endswith(".sql")
    }
    for migration in migrations:
        if (
            not migration.endswith(".down.sql")
            and migration.removesuffix(".sql") + ".down.sql" not in migrations
        ):
            raise ValueError(f"migration rollback pair missing from wheel: {migration}")


def verify_installed(checkout: Path) -> None:
    """Prove imports originate in this venv with networking forbidden."""
    distribution = importlib.metadata.distribution("ava")
    direct_url = distribution.read_text("direct_url.json")
    if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"):
        raise ValueError("ava is installed editably")
    for entry in sys.path:
        if entry and Path(entry).resolve().is_relative_to(checkout.resolve()):
            raise ValueError(f"checkout leaked onto isolated sys.path: {entry}")

    blocked = RuntimeError("runtime wheel import attempted a network connection")
    with (
        patch("socket.socket.connect", side_effect=blocked),
        patch("socket.socket.connect_ex", side_effect=blocked),
        patch("socket.create_connection", side_effect=blocked),
    ):
        for name in (
            "cli.main",
            "cli.python_install",
            "agent.exec_child",
            "ops.spec",
            "services.agent_host.daemon",
            "gateway.app",
        ):
            module = importlib.import_module(name)
            if module.__file__ is None:
                raise ValueError(f"{name} has no installed source file")
            origin = Path(module.__file__).resolve()
            if not origin.is_relative_to(Path(sys.prefix).resolve()):
                raise ValueError(f"{name} imported outside generation venv: {origin}")
    for member in REQUIRED:
        if not Path(str(distribution.locate_file(member))).is_file():
            raise ValueError(f"installed runtime member missing: {member}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--checkout", type=Path)
    args = parser.parse_args()
    verify_members(args.wheel)
    if args.checkout is not None:
        verify_installed(args.checkout)
    print("runtime wheel verification passed")


if __name__ == "__main__":
    main()
