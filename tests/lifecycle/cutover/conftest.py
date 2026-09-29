"""Disposable legacy-born homes and a fake OS scheduler for the cutover scripts.

A `LegacyHome` reproduces the layout legacy code left on production hosts (the
fleet cutover plan, "Production facts" and Appendix B): `.env` keys with
secret-looking values, `machine_*` identity files, a gateway-shaped registry
record (also on a remote runner, P7), legacy state files and pidfiles, an inert
resumed pause-owner journal, former-gateway residue on a runner, and legacy
OS jobs: launchd plists and loaded labels, crontab lines and systemd unit
files. A sibling home whose slug starts with this home's slug registers its
own jobs in the same namespaces; nothing may ever touch them.

The scheduler is faked end to end: `launchctl`, `crontab`, `systemctl` and
`sudo` are small Python executables that read and write state files under the
test's temporary directory, so no real launchd label, crontab line or unit is
ever touched. Every label and marker derives from the temporary home's slug.
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import pytest

from base.cluster import home_slug
from base.host.env.port_block import PORT_OFFSETS
from base.sessions.env_forwarding import normalize_service_path
from scripts.cutover_legacy_jobs import Host

CANARY = "canary-3f9a-never-printed"
MACHINE_KEY = "OPENAI_API_KEY"

_LAUNCHCTL = """
import pathlib, sys
S = pathlib.Path({state!r})
rows = [r for r in (S / "launchd").read_text().splitlines() if r] if (S / "launchd").exists() else []
with open(S / "calls", "a") as log:
    log.write(" ".join(["launchctl", *sys.argv[1:]]) + "\\n")
if sys.argv[1] == "list":
    print("PID\\tStatus\\tLabel")
    for row in rows:
        print(row)
    sys.exit(0)
if sys.argv[1] == "bootout":
    label = sys.argv[2].rsplit("/", 1)[1]
    kept = [r for r in rows if r.split("\\t")[2] != label]
    if len(kept) == len(rows):
        print("Boot-out failed: 3: No such process", file=sys.stderr)
        sys.exit(3)
    (S / "launchd").write_text("".join(r + "\\n" for r in kept))
    sys.exit(0)
sys.exit(1)
"""

_CRONTAB = """
import pathlib, sys
S = pathlib.Path({state!r})
with open(S / "calls", "a") as log:
    log.write(" ".join(["crontab", *sys.argv[1:]]) + "\\n")
if sys.argv[1] == "-l":
    if not (S / "crontab").exists():
        print("crontab: no crontab for tester", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write((S / "crontab").read_text())
    sys.exit(0)
if sys.argv[1] == "-":
    (S / "crontab").write_text(sys.stdin.read())
    sys.exit(0)
sys.exit(1)
"""

_LOGGER = """
import pathlib, subprocess, sys
S = pathlib.Path({state!r})
with open(S / "calls", "a") as log:
    log.write(" ".join([{name!r}, *sys.argv[1:]]) + "\\n")
if {name!r} == "sudo":
    assert sys.argv[1] == "-n", sys.argv
    if sys.argv[2] == "rm":
        sys.exit(subprocess.run(sys.argv[2:]).returncode)
sys.exit(0)
"""


def _script(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return str(path)


@dataclass
class FakeScheduler:
    root: Path
    platform: str

    @property
    def state(self) -> Path:
        return self.root / "state"

    def host(self) -> Host:
        bins = self.root / "bin"
        bins.mkdir(parents=True, exist_ok=True)
        state = str(self.state)
        return Host(
            platform=self.platform,
            uid=501,
            launch_agents=self.root / "LaunchAgents",
            user_units=self.root / "user-units",
            system_units=self.root / "system-units",
            crontab_lock=self.root / "crontab.lock",
            commands=(
                ("launchctl", _script(bins / "launchctl", _LAUNCHCTL.format(state=state))),
                ("crontab", _script(bins / "crontab", _CRONTAB.format(state=state))),
                (
                    "systemctl",
                    _script(bins / "systemctl", _LOGGER.format(state=state, name="systemctl")),
                ),
                ("sudo", _script(bins / "sudo", _LOGGER.format(state=state, name="sudo"))),
            ),
        )

    def load(self, label: str, pid: int | None = None) -> None:
        with (self.state / "launchd").open("a") as rows:
            rows.write(f"{pid if pid is not None else '-'}\t0\t{label}\n")

    def loaded(self) -> set[str]:
        path = self.state / "launchd"
        return (
            {row.split("\t")[2] for row in path.read_text().splitlines() if row}
            if path.exists()
            else set()
        )

    def crontab(self) -> list[str]:
        path = self.state / "crontab"
        return path.read_text().splitlines() if path.exists() else []

    def calls(self) -> list[str]:
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []


def _free_block() -> int:
    """A block base whose every port is free on loopback."""
    for _ in range(200):
        base = 30000 + secrets.randbelow(1000) * (len(PORT_OFFSETS) + 1)
        if all(_port_free(base + offset) for offset in PORT_OFFSETS.values()):
            return base
    raise RuntimeError("no free port block for the legacy home")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _stale_pid() -> int:
    pid = 4_000_000
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@dataclass
class LegacyHome:
    """One disposable legacy-shaped home plus its sibling and scheduler."""

    home: Path
    registry: Path
    scheduler: FakeScheduler
    roles: tuple[str, ...]
    ports: dict[str, int]
    sibling: Path
    children: list[subprocess.Popen[bytes]] = field(default_factory=list[subprocess.Popen[bytes]])

    @property
    def slug(self) -> str:
        return home_slug(self.home)

    @property
    def checkout(self) -> Path:
        return self.home / "source"

    @property
    def gateway(self) -> bool:
        return "gateway" in self.roles

    def env(self) -> dict[str, str]:
        from dotenv import dotenv_values

        return {k: v or "" for k, v in dotenv_values(self.home / ".env").items()}

    def registry_records(self) -> dict[str, dict[str, Any]]:
        return json.loads(self.registry.read_text())

    def spawn(self, token: str, *, cwd: Path | None = None) -> subprocess.Popen[bytes]:
        """A stand-in legacy service: its command line names an Ava module."""
        child = subprocess.Popen(  # noqa: S603 — this interpreter, fixed argv
            [sys.executable, "-c", "import time; time.sleep(120)", token],
            cwd=cwd or self.checkout,
        )
        self.children.append(child)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if token in " ".join(psutil.Process(child.pid).cmdline()):
                    return child
            except psutil.Error:
                pass
            time.sleep(0.02)
        return child

    def snapshot(self) -> dict[str, str]:
        """Every path under the home and scheduler state, with its content digest."""
        import hashlib

        out: dict[str, str] = {}
        for root in (self.home, self.scheduler.root / "LaunchAgents", self.scheduler.state):
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.name.endswith(".lock") and path.name != "calls":
                    out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        out["registry"] = self.registry.read_text()
        return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _record(home: Path, ports: dict[str, int]) -> dict[str, object]:
    return {
        "ports": ports,
        "gateway_home": str(home),
        "created_at": "2026-08-01T00:00:00+00:00",
        "data_plane_host": "",
    }


def _identity(home: Path, roles: tuple[str, ...]) -> None:
    _write(home / "machine_name", "legacy-box")
    _write(home / "machine_host", "10.0.0.7")
    for cap in ("gateway", "agent_runner", "observability_station"):
        flag = cap.replace("_", "-") in roles
        _write(home / f"machine_serve_{cap}", "true" if flag else "false")


def _env(legacy: LegacyHome) -> None:
    ports = legacy.ports
    lines = {
        "AVA_CLUSTER_SECRET": CANARY,
        "AVA_REDIS_ADMIN_PASSWORD": CANARY,
        "AVA_CLUSTER": "main",
        "AVA_RESTARTER_HEALTH_PORT": "8102",
        "AVA_TRACK_MODE": "releases",
        MACHINE_KEY: CANARY,
    }
    if legacy.gateway:
        lines |= {
            "AVA_DB_URL": f"postgresql://ava_main@127.0.0.1:{ports['pgbouncer']}/ava_main",
            "AVA_REDIS_URL": f"redis://ava:{CANARY}@127.0.0.1:{ports['redis']}/0",
            "AVA_DB_ADMIN_PASSWORD": CANARY,
            "AVA_GATEWAY_PORT": str(ports["gateway"]),
        }
    else:
        lines |= {
            "AVA_GATEWAY_URL": "http://gateway.test:8000",
            "AVA_DB_ADMIN_PASSWORD": CANARY,
            "AVA_RUNNER_DB_PASSWORD": CANARY,
            "AVA_REDIS_PASSWORD": CANARY,
            "AVA_PITR_GCS_BUCKET": "legacy-bucket",
            "AVA_PITR_BACKUP_KEY_FILE": "/legacy/pg-backup.key",
        }
    _write(legacy.home / ".env", "".join(f"{k}={v}\n" for k, v in lines.items()))


def _state(legacy: LegacyHome) -> None:
    home = legacy.home
    for name in ("installed_sha", "deploy-state.json", "cluster_paused", "health_probe_failures"):
        _write(home / name, "legacy\n")
    for name in ("updating.flag", "updater.lock", "updater-handoff.lock", "lifecycle-op.json"):
        _write(home / "run" / name, "legacy\n")
    _write(home / "deploy-state.lifecycle.lock", "")
    _write(home / "run" / "sessions" / "agent-host.json", "{}")
    _write(home / "run" / "session-code" / "agent-host", "x")
    _write(home / "state" / "hold-watchdog-attempt", "{}")
    _write(home / "run" / "agent-host.pid", f"{_stale_pid()}\n")
    _write(home / "run" / "watchdog-agent-runner.pid", "not-a-pid\n")
    _write(home / "disabled_services", "memory_indexer\n" if legacy.gateway else "")
    pause = {
        "state": "resumed",
        "holder": "ubuntu:pid2669750",
        "acquired_at": "2026-09-24T10:00:00+00:00",
        "maintenance": {
            "phase": "drained",
            "commands": {"7": 11},
            "drained": [7],
            "failures": {},
            "parked": [],
        },
    }
    _write(home / "run" / "deploy-pause-owner.json", json.dumps(pause))
    _write(home / "source" / "README", "checkout\n")
    (home / "helper").mkdir()


def _residue(home: Path) -> None:
    _write(home / "secrets" / "pg-backup.key", CANARY)
    _write(home / "secrets" / "gcs-uploader.json", CANARY)
    _write(home / "backups" / "db" / "dump.enc", CANARY)
    _write(home / "physical-backup" / "spool" / "x", "wal")
    _write(home / "masked-backup-20260920" / "dump.sql", "masked")
    _write(home / "redis" / "dump.rdb", "rdb")
    _write(home / "pgbouncer" / "userlist.txt", f'"ava_main" "{CANARY}"')
    _write(home / "run" / "bootstrap-snapshot.json", json.dumps({"AVA_DB_URL": CANARY}))


def _plist(scheduler: FakeScheduler, label: str) -> None:
    _write(scheduler.root / "LaunchAgents" / f"{label}.plist", f"<plist>{label}</plist>\n")


def _jobs(legacy: LegacyHome) -> None:
    scheduler, slug, sibling = legacy.scheduler, legacy.slug, home_slug(legacy.sibling)
    ours = [
        f"com.ava.{slug}.watchdog-probe.agent-runner",
        f"com.ava.{slug}.hold-watchdog",
        f"com.ava.{slug}.logs-maintenance",
        f"com.ava.{slug}.packages-refresh",
    ]
    for label in ours:
        _plist(scheduler, label)
        scheduler.load(label)
    _plist(scheduler, f"com.ava.{slug}.autostart")  # present, not loaded (P3)
    _plist(scheduler, f"com.ava.permissions-helper.{slug}")
    for label in (
        f"com.ava.{sibling}.watchdog-probe.agent-runner",
        f"com.ava.{sibling}.health-probe",
    ):
        _plist(scheduler, label)
        scheduler.load(label)
    home, other = legacy.home, legacy.sibling
    # This home's health probe is already unregistered (runbook W1, which only
    # the old gateway's own start undoes): `arm_health_probe` registers it again.
    cron = [
        f"* * * * * AVA_HOME={home} /x/ava cluster watchdog-probe --role gateway  # ava-watchdog-probe.gateway.{slug}",
        f"*/5 * * * * AVA_HOME={home} /x/ava cluster hold-watchdog  # ava-hold-watchdog.{slug}",
        f"@reboot AVA_HOME={home} /x/ava boot  # ava-autostart.{slug}",
        f"*/5 * * * * AVA_HOME={other} /y/ava cluster health-probe  # ava-health-probe.{sibling}",
        f"* * * * * AVA_HOME={other} /y/ava cluster watchdog-probe --role gateway  # ava-watchdog-probe.gateway.{sibling}",
        "0 * * * * /usr/local/bin/memory-pull.sh",
    ]
    _write(scheduler.state / "crontab", "".join(f"{line}\n" for line in cron))
    units = {
        scheduler.root / "user-units" / f"com.ava.gate.{slug}.service": "[Unit]\n",
        scheduler.root / "user-units" / f"com.ava.loki.{slug}.service": "[Unit]\n",
        scheduler.root / "user-units" / f"com.ava.loki.{sibling}.service": "[Unit]\n",
        scheduler.root
        / "system-units"
        / f"ava-boot.{slug}.service": "Description=Ava cluster boot convergence\n",
        scheduler.root / "system-units" / f"ava-boot.{sibling}.service": "[Unit]\n",
    }
    for path, text in units.items():
        _write(path, text)


def arm_health_probe(legacy: LegacyHome) -> str:
    """Register this home's legacy auto-rollback probe again, as every start of the
    old gateway does; returns the crontab line."""
    line = (
        f"*/5 * * * * AVA_HOME={legacy.home} /x/ava cluster health-probe --auto-rollback "
        f"--threshold 3 # ava-health-probe.{legacy.slug}"
    )
    crontab = legacy.scheduler.state / "crontab"
    crontab.write_text(crontab.read_text() + line + "\n")
    return line


def build_legacy_home(root: Path, roles: tuple[str, ...], platform: str) -> LegacyHome:
    root.mkdir(parents=True)
    root = root.resolve()
    home = root / f".ava-legacy{secrets.token_hex(3)}"
    home.mkdir(mode=0o700)
    sibling = root / f".{home_slug(home)}-sibling"
    sibling.mkdir(mode=0o700)
    scheduler = FakeScheduler(root / "os", platform)
    scheduler.state.mkdir(parents=True)
    base = _free_block()
    ports = {name: base + offset for name, offset in PORT_OFFSETS.items()}
    sibling_base = _free_block()
    registry = root / "clusters.json"
    registry.write_text(
        json.dumps(
            {
                str(home): _record(home, ports),
                str(sibling): _record(
                    sibling, {name: sibling_base + offset for name, offset in PORT_OFFSETS.items()}
                ),
            },
            indent=2,
        )
    )
    legacy = LegacyHome(home, registry, scheduler, roles, ports, sibling)
    _identity(home, roles)
    _env(legacy)
    _state(legacy)
    if not legacy.gateway:
        _residue(home)
    _jobs(legacy)
    return legacy


def record_repair(home: Path, state: str) -> None:
    """A database-records (W7) journal of this adoption whose one run is `state`."""
    from scripts import cutover_db_records as records

    run = {"state": state, "adoption": records.adoption(home.resolve())}
    journal = {"version": records.VERSION, "home": str(home.resolve()), "runs": [run]}
    (home / records.JOURNAL).parent.mkdir(parents=True, exist_ok=True)
    (home / records.JOURNAL).write_text(json.dumps(journal))


# The reviewed --service-path must already be normalized (admit_service_path).
# On a usrmerge Linux /bin and /sbin resolve to /usr/bin and /usr/sbin, so the
# raw macOS-shaped list is not; normalize it on the host running the suite.
SERVICE_PATH = normalize_service_path(
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)


@pytest.fixture
def make_legacy(tmp_path: Path) -> Iterator[object]:
    made: list[LegacyHome] = []

    def factory(roles: tuple[str, ...] = ("agent-runner",), platform: str = "darwin") -> LegacyHome:
        legacy = build_legacy_home(tmp_path / f"h{len(made)}", roles, platform)
        made.append(legacy)
        return legacy

    try:
        yield factory
    finally:
        for legacy in made:
            for child in legacy.children:
                child.kill()
                child.wait(timeout=5)
