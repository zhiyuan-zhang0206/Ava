"""The legacy OS jobs the one-time production cutover retires, matched by exact home slug.

Legacy code registered its scheduled and supervised jobs under the shared
`com.ava.*` (launchd, systemd) and `# ava-*` (crontab) namespaces. Other
clusters on the same host (development, preview and rehearsal homes) register
into the same namespaces, so a job belongs to the home under conversion only
when its launchd label or unit name equals the name derived from that home's
slug, or its crontab marker is the exact whitespace-delimited token
`# ava-<kind>.<slug>`. Prefix or substring matches are never ownership: a
sibling home whose slug starts with this slug keeps its jobs.

`TABLE` is the explicit retirement table (the fleet cutover plan, section
"Legacy OS jobs"). The disarm order puts the actors that can run the retired
updater or roll back first: the gateway health probe (`--auto-rollback`), the
watchdog probes (they revive watchdogs that host the pin and schema
self-update controllers) and the hold watchdog (it runs hold recovery). The
health probe must already be gone when evidence of a stopped home is taken
(`Jobs.health_probe_refusal`). The permissions helper is kept; the new
converge rebuilds, re-signs and reloads it.

Some labels are reused by the current code (health probe, autostart, logs,
packages, PR flow, the Linux boot unit): after adoption and a first start they
name current registrations, not legacy residue. The adoption journal, not the
job content, records which ones were retired.

This module only reads the scheduler state and performs the exact retirement
primitives; the journal lives in `scripts/cutover_adopt_home.py`. It is
deleted with the other cutover scripts after the cutover.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from shared.cluster import home_slug
from shared.platform import file_lock

CAPABILITIES = ("gateway", "agent-runner", "observability-station")
LGTM_BACKENDS = ("loki", "prometheus", "grafana")


@dataclass(frozen=True)
class JobKind:
    """One row of the retirement table."""

    kind: str
    retire: bool
    # The current code registers the same name; post-adoption it is not residue.
    reused: bool


TABLE = (
    JobKind("health-probe", retire=True, reused=True),
    JobKind("watchdog-probe", retire=True, reused=False),
    JobKind("hold-watchdog", retire=True, reused=False),
    JobKind("autostart", retire=True, reused=True),
    JobKind("gate", retire=True, reused=False),
    JobKind("lgtm", retire=True, reused=False),
    JobKind("logs-maintenance", retire=True, reused=True),
    JobKind("packages-refresh", retire=True, reused=True),
    JobKind("pr-flow", retire=True, reused=True),
    JobKind("permissions-helper", retire=False, reused=True),
)
_KINDS = {row.kind: row for row in TABLE}
_ORDER = {row.kind: index for index, row in enumerate(TABLE)}

# Crontab commands legacy code wrote before lines carried a slug marker.
_UNMARKED_COMMANDS = re.compile(
    r"cluster health-probe|health-probe-cron|cluster watchdog-probe|cluster hold-watchdog|/ava boot\b"
)


@dataclass(frozen=True)
class Host:
    """This OS user's scheduler state and the binaries that drive it.

    Tests construct one over temporary directories and fake binaries; the
    production value is `Host.current()`.
    """

    platform: str
    uid: int
    launch_agents: Path
    user_units: Path
    system_units: Path
    crontab_lock: Path
    commands: tuple[tuple[str, str], ...]

    @classmethod
    def current(cls) -> Host:
        from shared.platform import user_systemd_unit_dir

        linux = sys.platform.startswith("linux")
        return cls(
            platform="linux" if linux else sys.platform,
            uid=os.getuid(),
            launch_agents=Path.home() / "Library" / "LaunchAgents",
            user_units=user_systemd_unit_dir() if linux else Path("/nonexistent"),
            system_units=Path("/etc/systemd/system"),
            crontab_lock=Path.home() / ".ava-crontab.lock",
            commands=tuple(
                (name, shutil.which(name) or name)
                for name in ("launchctl", "crontab", "systemctl", "sudo")
            ),
        )

    def binary(self, name: str) -> str:
        return dict(self.commands)[name]

    def has(self, name: str) -> bool:
        return Path(self.binary(name)).is_file()

    def run(self, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — fixed scheduler binaries, no shell
            [self.binary(argv[0]), *argv[1:]],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )


@dataclass(frozen=True)
class LaunchdJob:
    kind: str
    label: str
    plist: str
    present: bool
    loaded: bool
    pid: int | None
    retire: bool
    reused: bool


@dataclass(frozen=True)
class CronLine:
    kind: str
    line: str
    marked: bool
    reused: bool


@dataclass(frozen=True)
class SystemdUnit:
    kind: str
    unit: str
    path: str
    scope: str
    reused: bool


@dataclass(frozen=True)
class Jobs:
    """Everything the scheduler holds for one home, plus what cannot be attributed."""

    launchd: tuple[LaunchdJob, ...]
    cron: tuple[CronLine, ...]
    units: tuple[SystemdUnit, ...]
    ambiguous: tuple[str, ...]
    crontab_error: str | None
    crontab: str | None

    def retirable(self, *, reused: bool = True) -> Jobs:
        """Only the rows the table retires; `reused=False` drops reused names."""
        return Jobs(
            launchd=tuple(
                job
                for job in self.launchd
                if job.retire and (job.present or job.loaded) and (reused or not job.reused)
            ),
            cron=tuple(line for line in self.cron if reused or not line.reused),
            units=tuple(unit for unit in self.units if reused or not unit.reused),
            ambiguous=self.ambiguous,
            crontab_error=self.crontab_error,
            crontab=self.crontab,
        )

    def empty(self) -> bool:
        return not (self.launchd or self.cron or self.units)

    def health_probe_refusal(self) -> str | None:
        """Why this home cannot be attested or adopted yet: a legacy health probe
        is registered for it. None when none is.

        W1 unregisters the probe before the first stop. Only the old code
        registers it again, and every start of the old gateway does (its
        lifespan re-registers it with `--auto-rollback`), so one found after W1
        means the old gateway started since. Armed, it rolls the stopped home
        back and starts the old code, after any evidence taken of that home.
        """
        entries = [
            *(job.label for job in self.launchd if job.kind == "health-probe"),
            *(line.line for line in self.cron if line.kind == "health-probe"),
        ]
        if not entries:
            return None
        return (
            f"the legacy health probe is registered ({'; '.join(entries)}): unregister it "
            "with the old code (`ava cluster health-probe-unregister`, runbook W1). Found "
            "after W1 it means the old gateway started again, which re-registers it; see "
            "conventions/cutover-home-adoption.md"
        )


def launchd_labels(slug: str) -> tuple[tuple[str, str], ...]:
    """Every `(kind, label)` the table knows for `slug`, in disarm order."""
    rows = [
        ("health-probe", f"com.ava.{slug}.health-probe"),
        *(("watchdog-probe", f"com.ava.{slug}.watchdog-probe.{cap}") for cap in CAPABILITIES),
        ("hold-watchdog", f"com.ava.{slug}.hold-watchdog"),
        ("autostart", f"com.ava.{slug}.autostart"),
        ("gate", f"com.ava.gate.{slug}"),
        *(("lgtm", f"com.ava.{name}.{slug}") for name in LGTM_BACKENDS),
        ("logs-maintenance", f"com.ava.{slug}.logs-maintenance"),
        ("packages-refresh", f"com.ava.{slug}.packages-refresh"),
        ("pr-flow", f"com.ava.{slug}.pr-flow"),
        ("permissions-helper", f"com.ava.permissions-helper.{slug}"),
    ]
    return tuple(rows)


def cron_markers(slug: str) -> tuple[tuple[str, str], ...]:
    """Every `(kind, marker)` a legacy crontab line carried for `slug`."""
    return (
        ("health-probe", f"# ava-health-probe.{slug}"),
        *(("watchdog-probe", f"# ava-watchdog-probe.{cap}.{slug}") for cap in CAPABILITIES),
        ("hold-watchdog", f"# ava-hold-watchdog.{slug}"),
        ("autostart", f"# ava-autostart.{slug}"),
        ("logs-maintenance", f"# ava-logs-maintenance.{slug}"),
        ("packages-refresh", f"# ava-packages-refresh.{slug}"),
        ("pr-flow", f"# ava-pr-flow.{slug}"),
    )


def systemd_units(slug: str) -> tuple[tuple[str, str, str], ...]:
    """Every `(kind, unit, scope)` legacy code installed for `slug` on Linux."""
    safe = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_.-"
    gate = "".join(chr(b) if b in safe else f"\\x{b:02x}" for b in slug.encode())
    lgtm = re.sub(r"[^A-Za-z0-9_.-]", "_", slug)
    return (
        ("autostart", f"ava-boot.{slug}.service", "system"),
        ("gate", f"com.ava.gate.{gate}.service", "user"),
        *(("lgtm", f"com.ava.{name}.{lgtm}.service", "user") for name in LGTM_BACKENDS),
    )


def _token(marker: str) -> re.Pattern[str]:
    """The marker as a whole whitespace-delimited token, never a prefix."""
    return re.compile(r"(?<!\S)" + re.escape(marker) + r"(?!\S)")


def _home_token(home: Path) -> re.Pattern[str]:
    return re.compile(r"(?<!\S)AVA_HOME=" + re.escape(str(home)) + r"(?!\S)")


def _loaded_launchd(host: Host) -> dict[str, int | None]:
    result = host.run("launchctl", "list")
    if result.returncode != 0:
        raise RuntimeError(f"launchctl list failed: {result.stderr.strip() or result.returncode}")
    loaded: dict[str, int | None] = {}
    for row in result.stdout.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) == 3:
            loaded[parts[2]] = int(parts[0]) if parts[0].isdigit() else None
    return loaded


def _discover_launchd(host: Host, slug: str) -> tuple[LaunchdJob, ...]:
    if host.platform != "darwin":
        return ()
    loaded = _loaded_launchd(host)
    jobs: list[LaunchdJob] = []
    for kind, label in launchd_labels(slug):
        plist = host.launch_agents / f"{label}.plist"
        row = _KINDS[kind]
        jobs.append(
            LaunchdJob(
                kind=kind,
                label=label,
                plist=str(plist),
                present=plist.exists(),
                loaded=label in loaded,
                pid=loaded.get(label),
                retire=row.retire,
                reused=row.reused,
            )
        )
    return tuple(job for job in jobs if job.present or job.loaded)


def read_crontab(host: Host) -> tuple[str | None, str | None]:
    """`(content, error)`: an absent crontab is empty content, never an error."""
    if not host.has("crontab"):
        return None, None
    result = host.run("crontab", "-l")
    if result.returncode == 0:
        return result.stdout, None
    if "no crontab" in (result.stderr or "").lower():
        return "", None
    return None, f"crontab -l failed: {result.stderr.strip() or result.returncode}"


def classify_crontab(content: str, home: Path, slug: str) -> tuple[list[CronLine], list[str]]:
    """This home's exact-marker lines, and unmarked Ava lines nobody can attribute."""
    markers = [(kind, _token(marker)) for kind, marker in cron_markers(slug)]
    home_token = _home_token(home)
    ours: list[CronLine] = []
    ambiguous: list[str] = []
    for line in content.splitlines():
        match = next((kind for kind, pattern in markers if pattern.search(line)), None)
        if match is not None:
            if "AVA_HOME=" in line and not home_token.search(line):
                ambiguous.append(line)  # our slug, another home: never guess
                continue
            ours.append(CronLine(match, line, marked=True, reused=_KINDS[match].reused))
            continue
        if "# ava-" in line or not _UNMARKED_COMMANDS.search(line):
            continue  # another cluster's marked line, or not an Ava job at all
        if home_token.search(line):
            kind = _unmarked_kind(line)
            ours.append(CronLine(kind, line, marked=False, reused=_KINDS[kind].reused))
        elif "AVA_HOME=" not in line:
            ambiguous.append(line)
    return ours, ambiguous


def _unmarked_kind(line: str) -> str:
    if "hold-watchdog" in line:
        return "hold-watchdog"
    if "watchdog-probe" in line:
        return "watchdog-probe"
    if "/ava boot" in line:
        return "autostart"
    return "health-probe"


def _discover_units(host: Host, slug: str) -> tuple[SystemdUnit, ...]:
    if host.platform != "linux":
        return ()
    units: list[SystemdUnit] = []
    for kind, unit, scope in systemd_units(slug):
        path = (host.system_units if scope == "system" else host.user_units) / unit
        if path.exists():
            units.append(SystemdUnit(kind, unit, str(path), scope, _KINDS[kind].reused))
    return tuple(units)


def discover(home: Path, host: Host) -> Jobs:
    """Read-only: every scheduler entry the table attributes to `home`."""
    slug = home_slug(home)
    content, error = read_crontab(host)
    cron: list[CronLine] = []
    ambiguous: list[str] = []
    if content is not None:
        cron, ambiguous = classify_crontab(content, home, slug)
    cron.sort(key=lambda line: _ORDER[line.kind])
    return Jobs(
        launchd=_discover_launchd(host, slug),
        cron=tuple(cron),
        units=_discover_units(host, slug),
        ambiguous=tuple(ambiguous),
        crontab_error=error,
        crontab=content,
    )


def _move(source: Path, destination: Path) -> None:
    from shared.private_storage import ensure_private_dir

    if not source.exists():
        return
    if destination.exists():
        raise RuntimeError(f"archive already holds {destination}; refusing to overwrite it")
    ensure_private_dir(destination.parent)
    source.rename(destination)


def retire_launchd(host: Host, job: LaunchdJob, archive: Path) -> None:
    """Boot the job out first, then move its plist into the archive."""
    result = host.run("launchctl", "bootout", f"gui/{host.uid}/{job.label}")
    if result.returncode != 0 and job.label in _loaded_launchd(host):
        raise RuntimeError(f"launchctl bootout {job.label} failed: {result.stderr.strip()}")
    _move(Path(job.plist), archive / Path(job.plist).name)


def remove_cron_lines(host: Host, lines: tuple[str, ...], archive: Path) -> int:
    """Remove exactly `lines` (whole-line equality) under the crontab lock."""
    from shared.private_storage import ensure_private_dir, write_private_bytes

    with file_lock(host.crontab_lock):
        content, error = read_crontab(host)
        if error is not None or content is None:
            raise RuntimeError(error or "crontab is not installed on this host")
        copy = archive / "crontab.before"
        if not copy.exists():
            ensure_private_dir(archive)
            write_private_bytes(copy, content.encode())
        kept = [line for line in content.splitlines() if line not in lines]
        removed = len(content.splitlines()) - len(kept)
        if removed == 0:
            return 0
        result = host.run("crontab", "-", stdin="".join(f"{line}\n" for line in kept))
        if result.returncode != 0:
            raise RuntimeError(f"crontab rewrite failed: {result.stderr.strip()}")
    return removed


def _checked(host: Host, *argv: str) -> None:
    result = host.run(*argv)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {result.stderr.strip()}")


def retire_unit(host: Host, unit: SystemdUnit, archive: Path) -> None:
    """Disable and stop the unit, then move (or copy, for a system unit) its file.

    Every manager call must succeed (a user manager without a bus, as over SSH
    without lingering, fails loudly before the file moves). Once the unit file
    is gone a re-run only reloads the manager: the retirement already ran.
    """
    from shared.private_storage import ensure_private_dir, write_private_bytes

    path = Path(unit.path)
    if unit.scope == "user":
        if path.exists():
            _checked(host, "systemctl", "--user", "disable", "--now", unit.unit)
            _move(path, archive / unit.unit)
        _checked(host, "systemctl", "--user", "daemon-reload")
        return
    if path.exists():
        copy = archive / unit.unit
        if not copy.exists():
            ensure_private_dir(archive)
            write_private_bytes(copy, path.read_bytes())
        _checked(host, "sudo", "-n", "systemctl", "disable", "--now", unit.unit)
        _checked(host, "sudo", "-n", "rm", "-f", str(path))
    _checked(host, "sudo", "-n", "systemctl", "daemon-reload")
