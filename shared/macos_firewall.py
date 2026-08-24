"""Audit and converge the macOS Application Firewall's per-binary allow list.

## The defect this observes (issue #949)

macOS's Application Firewall (ALF) allows incoming connections per *binary*, and
every binary Ava serves an off-box port from is one macOS considers unknown:

    $ codesign -dv .../uv/python/cpython-3.12.11-macos-aarch64-none/bin/python3.12
    Identifier=-
    Signature=adhoc

ALF auto-allows built-in and *Developer-ID*-signed software (`allowsignedenabled`
/ `allowdownloadsignedenabled`, both on by default), and an ad-hoc linker-signed
interpreter is neither. So it needs an explicit rule — and the rule is stored
against the path it was added with, while every path here is **version-stamped**:

    ~/.local/share/uv/python/cpython-3.12.11-macos-aarch64-none/bin/python3.12
    ~/.ava/runtime/pg/17.4.0/bin/postgres

A `uv python` bump (or a `_PG_VERSION` bump) therefore does not invalidate the
rule so much as *orphan* it: the rule still names the old path, the process now
runs from a new one, and ALF has never heard of the new one. Loopback is never
filtered, so the gateway keeps answering `127.0.0.1` perfectly while every
off-box dial is dropped. Nothing in the process notices, because nothing in the
process is wrong.

## Why repair is unprivileged-first

Empirical verification on macOS 15.3.1 showed that all three manifest mutations
(`--add`, `--unblockapp`, and `--remove`) succeed as the ordinary Ava user.
Converge therefore calls `socketfilterfw` directly first. The commands are
idempotent, so every start can repair a new version-stamped binary before its
next bind without prompting.

Other macOS releases may still reject an unprivileged mutation. That platform
difference degrades through two bounded fallbacks: retry the same command with
`sudo -n`, then report the exact manual `sudo` command when that also fails.
`sudo -n` never opens a password prompt, so an unattended `ava start` cannot
hang. An older host can optionally retain the narrow `AVA_FIREWALL` sudoers
grant; without it, converge reports but does not crash. Queries
(`--getglobalstate` and `--listapps`) remain unprivileged on every supported
path.

## Why `--listapps` and not `--getappblocked`

`--getappblocked` answers the *effective* question, and so cannot see the defect:

    $ socketfilterfw --getappblocked .../cpython-3.12.11-.../bin/python3.12
    Incoming connection to .../python3.12 is permitted.

That path has no rule whatsoever — it is absent from `--listapps` — and the
answer is still "permitted", because with no rule the default policy allows.
"Permitted by an explicit rule" and "permitted because nothing filters it yet"
are indistinguishable through that flag, and they diverge the moment the
firewall's posture changes. `--listapps` enumerates the rules themselves, so
absence is observable, and a rule that exists but says *Block* is observable too
— a distinct state needing `--unblockapp`, which `--add` alone would not fix.
"""

from __future__ import annotations

import contextlib
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from shared.proc import run_bounded

SOCKETFILTERFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"

# Both queries are pure reads that answer as an unprivileged user. Short timeout:
# socketfilterfw talks to a local daemon and answers in milliseconds, and this
# runs inside converge, where a hang is worse than an unknown.
_QUERY_TIMEOUT_S = 10.0

# `--listapps` prints an index line per rule followed by an indented state line:
#
#     3 : /usr/bin/python3
#                  (Allow incoming connections)
#
# The index line is what carries the path. Anchored on the leading `N :` so a
# path containing " : " cannot be truncated by a greedy split.
_LISTAPPS_ENTRY = re.compile(r"^\s*\d+\s*:\s*(?P<path>\S.*?)\s*$")
_LISTAPPS_STATE = re.compile(r"^\s+\((?P<state>Allow|Block) incoming connections\)\s*$")

# Addresses that mean "this host serves nothing off-box". ALF does not filter
# loopback, so a cluster that dials only these needs no rule at all and must not
# be nagged about one.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"})


class FirewallVerdict(StrEnum):
    """What the audit found. Only `RULES_MISSING` asks the operator for anything.

    The four quiet verdicts are not one "OK" value because they are quiet for
    different reasons, and a caller explaining an `OFF_BOX_UNREACHABLE` rollout
    needs to tell them apart: `FIREWALL_OFF` *rules the firewall out* as the
    cause and redirects the operator to the address configuration, which a bare
    boolean could not do.
    """

    NOT_MACOS = "not_macos"
    LOOPBACK_ONLY = "loopback_only"
    FIREWALL_OFF = "firewall_off"
    ALLOWED = "allowed"
    RULES_MISSING = "rules_missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class FirewallAudit:
    """One audit's result: the verdict, the binaries that need a rule, and why."""

    verdict: FirewallVerdict
    detail: str
    missing: tuple[Path, ...] = ()

    @property
    def needs_operator(self) -> bool:
        return self.verdict is FirewallVerdict.RULES_MISSING

    def repair_commands(self) -> tuple[str, ...]:
        """The exact privileged commands that repair this host, in order.

        Both verbs per binary, deliberately. `--add` creates the rule; a rule can
        also exist in the *Block* state (a past denial, or a stale entry ALF
        re-pointed), which `--add` leaves blocking. `--unblockapp` is idempotent
        on an already-allowed rule, so emitting both is correct in every case and
        conditional emission would only be correct in some.
        """
        return tuple(
            f'sudo "{SOCKETFILTERFW}" {verb} "{path}"'
            for path in self.missing
            for verb in ("--add", "--unblockapp")
        )


def _query(*args: str) -> str | None:
    """Run one read-only `socketfilterfw` query. None when it cannot be asked.

    Every failure mode collapses to None on purpose: this module's callers report
    rather than enforce, and "I could not read the firewall" is a single fact for
    them regardless of whether the tool is absent (a non-macOS host, or a macOS
    that moved it), refused to run, or hung.
    """
    if shutil.which(SOCKETFILTERFW) is None and not Path(SOCKETFILTERFW).is_file():
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed system tool at an absolute path, literal args
            [SOCKETFILTERFW, *args],
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def firewall_enabled() -> bool | None:
    """Is ALF filtering at all? None when the state cannot be read.

    `--getglobalstate` prints `Firewall is enabled. (State = 1)` or
    `Firewall is disabled. (State = 0)`; the `State = N` tail is matched rather
    than the prose because it is the machine-readable half of the same line.
    """
    out = _query("--getglobalstate")
    if out is None:
        return None
    match = re.search(r"State\s*=\s*(\d+)", out)
    if match is None:
        return None
    return match.group(1) != "0"


def allowlisted_paths() -> dict[str, bool] | None:
    """Every path ALF holds a rule for, mapped to True=Allow / False=Block.

    None when the list cannot be read. Note the list also contains Apple's own
    built-in service entries (`/usr/sbin/smbd`, `/usr/libexec/sharingd`, …), which
    is harmless here: this is only ever consulted for membership of paths Ava owns.
    """
    out = _query("--listapps")
    if out is None:
        return None
    rules: dict[str, bool] = {}
    pending: str | None = None
    for line in out.splitlines():
        state = _LISTAPPS_STATE.match(line)
        if state is not None and pending is not None:
            rules[pending] = state.group("state") == "Allow"
            pending = None
            continue
        entry = _LISTAPPS_ENTRY.match(line)
        # A rule whose state line never arrives is dropped rather than guessed:
        # membership with an unknown state is exactly the ambiguity `--getappblocked`
        # was rejected for.
        pending = entry.group("path") if entry is not None else pending
    return rules


def _covered(binary: Path, rules: dict[str, bool]) -> bool:
    """Does an Allow rule cover `binary`, named either literally or by its real path?

    Both forms are accepted because both are legitimately how a rule gets written,
    and the paths Ava resolves are layers of symlink over a versioned target:
    `$(brew --prefix pgbouncer)/bin/pgbouncer` is the stable
    `/opt/homebrew/opt/pgbouncer/…` symlink onto a versioned Cellar path, and
    `.venv/bin/python` is a symlink onto the uv interpreter. An operator (or
    this repo's own older runbook text) adds whichever form they had in hand.

    Accepting either is the asymmetry worth choosing deliberately. A false *alarm*
    is expensive — it prints a scary block on a host that is fine, on every
    `ava start`, until an operator learns to ignore this step, at which point the
    real occurrence is invisible too. A false *silence* costs one `OFF_BOX_UNREACHABLE`
    report, which now runs this same audit and will at least say the firewall was
    checked. So when the evidence is ambiguous, stay quiet.
    """
    if rules.get(str(binary), False):
        return True
    return rules.get(str(binary.resolve()), False)


def audit_allowlist(required: tuple[Path, ...], *, machine_host: str) -> FirewallAudit:
    """Does ALF currently let off-box connections reach each of `required`?

    `required` is the set of binaries this host serves a non-loopback port from,
    resolved by the caller through the same helpers that *launch* them — auditing
    a path the data plane does not actually run would prove nothing.

    Ordered so the cheapest disqualification wins: a non-macOS host, then a host
    that serves nothing off-box, then a firewall that filters nothing. Only a host
    that is all three of macOS, off-box-reachable and filtering can have the defect.
    """
    if platform.system() != "Darwin":
        return FirewallAudit(FirewallVerdict.NOT_MACOS, "not a macOS host")
    if machine_host.strip().lower() in LOOPBACK_HOSTS:
        return FirewallAudit(
            FirewallVerdict.LOOPBACK_ONLY,
            f"this host is reachable only at {machine_host} — ALF does not filter loopback",
        )
    enabled = firewall_enabled()
    if enabled is None:
        return FirewallAudit(
            FirewallVerdict.UNREADABLE, f"could not read the firewall state from {SOCKETFILTERFW}"
        )
    if not enabled:
        return FirewallAudit(
            FirewallVerdict.FIREWALL_OFF, "the Application Firewall is off — it blocks nothing"
        )
    rules = allowlisted_paths()
    if rules is None:
        return FirewallAudit(
            FirewallVerdict.UNREADABLE, f"could not read the allow list from {SOCKETFILTERFW}"
        )
    missing = tuple(p for p in required if not _covered(p, rules))
    if not missing:
        return FirewallAudit(
            FirewallVerdict.ALLOWED, f"all {len(required)} serving binaries are allow-listed"
        )
    return FirewallAudit(
        FirewallVerdict.RULES_MISSING,
        f"{len(missing)} of {len(required)} serving binaries have no ALF allow rule, "
        f"so off-box connections to them are dropped while loopback still works",
        missing=tuple(p.resolve() for p in missing),
    )


# --- declarative allowlist manifest ---------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    """One family of binaries the manifest manages.

    ``paths`` are glob patterns (``~`` expanded at resolve time) that point at
    the executables ALF must allow; every one is written as a glob because every
    Ava-managed path is version-stamped (`Cellar/pgbouncer/1.25.1/`,
    `runtime/pg/17.4.0/`, `uv/python/cpython-3.12.12-.../`). ``machine`` filters
    the entry to one machine (``shared.machine.machine_name``, e.g. "my-mac");
    None applies to every macOS host. ``purpose`` is operator-facing context for
    why the binary accepts inbound traffic. The filter exists for user
    applications an operator decided to allow inbound on a specific machine —
    Ava's own stack is unfiltered.
    """

    id: str
    paths: tuple[str, ...]
    purpose: str
    machine: str | None = None


# The whitelist Ava converges every macOS host to. Captured from a macOS
# host's manual allowlist: Apple's own signed
# entries (syslogd, sshd, cupsd, smbd, /usr/bin/python3, ...) are deliberately
# absent — ALF auto-allows them and never prompts for them. Resolving the globs
# at converge time adds a rule for a *new* version's path before anything binds
# it, which is what stops the "allow incoming connections?" popup storm on every
# update.
FIREWALL_MANIFEST: tuple[ManifestEntry, ...] = (
    ManifestEntry(
        "uv interpreter",
        ("~/.local/share/uv/python/*/bin/python3.*",),
        "Runs Ava Python services that accept inbound connections",
    ),
    ManifestEntry(
        "prod venv interpreter",
        ("~/.ava/source/.venv/bin/python3*",),
        "Covers the production virtualenv's listener entry points",
    ),
    ManifestEntry(
        "dev clone venv interpreter",
        ("~/Ava/.venv/bin/python3*",),
        "Covers development virtualenv listener entry points",
    ),
    ManifestEntry(
        "vendored postgres",
        ("~/.ava/runtime/pg/*/bin/postgres",),
        "Accepts Postgres clients on gateway hosts",
    ),
    ManifestEntry(
        "homebrew postgres",
        ("/opt/homebrew/Cellar/postgresql@17/*/bin/postgres",),
        "Accepts Postgres clients when the Homebrew binary is active",
    ),
    # Redis always binds loopback-only (task #1469), so it serves no off-box port
    # and needs no ALF rule. The private-network :6380 listener is the
    # `com.ava.redis-bridge` relay running as `/usr/bin/python3 relay.py`; that
    # Apple-signed built-in interpreter is auto-allowed and needs no manifest entry.
    ManifestEntry(
        "pgbouncer",
        (
            "/opt/homebrew/Cellar/pgbouncer/*/bin/pgbouncer",
            "/opt/homebrew/opt/pgbouncer/bin/pgbouncer",
        ),
        "Accepts pooled Postgres clients on gateway hosts",
    ),
    ManifestEntry(
        "homebrew python",
        (
            "/opt/homebrew/Cellar/python@3.*/*/Frameworks/Python.framework/"
            "Versions/*/Resources/Python.app/Contents/MacOS/Python",
        ),
        "Runs Homebrew Python listeners used by local services",
    ),
    ManifestEntry(
        "node",
        ("/opt/homebrew/Cellar/node/*/bin/node",),
        "Serves frontend and browser-tooling connections",
    ),
    ManifestEntry(
        "playwright chromium headless shell",
        (
            "~/Library/Caches/ms-playwright/chromium_headless_shell-*/"
            "chrome-headless-shell-mac-*/chrome-headless-shell",
        ),
        "Runs Playwright's headless browser endpoint",
    ),
    ManifestEntry(
        "homebrew adb",
        ("/opt/homebrew/bin/adb",),
        "Accepts Android Debug Bridge connections",
    ),
    ManifestEntry(
        "grafana",
        (
            "~/.ava/lgtm/native/grafana-home/bin/grafana",
            "/opt/homebrew/Cellar/grafana/*/bin/grafana",
        ),
        "Serves Grafana dashboards",
    ),
    ManifestEntry(
        "loki",
        ("~/.ava/lgtm/native/bin/loki",),
        "Accepts Loki telemetry",
    ),
    ManifestEntry(
        "otel collector",
        ("~/.ava/otel-collector/otelcol-contrib",),
        "Accepts OTLP telemetry",
    ),
    # (An operator can add machine-scoped entries for their own apps; see the
    # ManifestEntry.machine filter above.)
)

# Path families the manifest used to manage, kept so stale-rule pruning can
# retire rules left behind when the path moved (the Android SDK adb moved to
# Homebrew) or was removed. Never resolved for adding — there is nothing to
# allow at a path that does not exist.
FIREWALL_LEGACY_FAMILY: tuple[str, ...] = ("~/Library/Android/sdk/platform-tools/adb",)


def _machine_name() -> str:
    """This host's stable machine identifier; never raises.

    ``shared.machine.machine_name`` (env > ``$AVA_HOME/machine_name``) is the
    authoritative source. When it is unset the host is not yet a configured Ava
    unit, so fall back to the normalized ComputerName, then the bare hostname —
    a fresh machine can still be matched by an operator who names entries after
    either.
    """
    from_ava = _ava_machine_name()
    if from_ava:
        return from_ava
    computer_name = _computer_name()
    if computer_name:
        return computer_name
    return socket.gethostname().split(".")[0].lower()


def _ava_machine_name() -> str | None:
    """The configured Ava machine identifier, or None when unset/unreadable."""
    from shared.machine import MachineNameMissing, machine_name

    with contextlib.suppress(ImportError, MachineNameMissing):
        return machine_name()
    return None


def _computer_name() -> str | None:
    """The normalized ComputerName, or None when it cannot be read."""
    try:
        proc = subprocess.run(
            ["scutil", "--get", "ComputerName"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    name = re.sub(r"[^A-Za-z0-9]+", "-", proc.stdout.strip()).strip("-").lower()
    return name or None


def _expand_glob(pattern: str) -> tuple[Path, ...]:
    """Expand one multi-segment glob pattern to its existing paths.

    ``Path.glob`` only accepts a wildcard in the last component, so split at the
    first wildcard: the prefix is literal, the rest is the glob. A pattern with
    no wildcard at all resolves to itself when it exists.
    """
    expanded = Path(pattern).expanduser()
    first_wild = next(
        (i for i, part in enumerate(expanded.parts) if any(c in part for c in "*?[")),
        None,
    )
    if first_wild is None:
        return (expanded,) if expanded.exists() else ()
    base = Path(*expanded.parts[:first_wild])
    rest = "/".join(expanded.parts[first_wild:])
    return tuple(h for h in base.glob(rest) if h.exists())


def manifest_paths() -> tuple[Path, ...]:
    """Every existing binary this host's manifest requires an allow rule for.

    Machine-filtered, glob-expanded, existence-checked, deduplicated and sorted.
    A pattern that matches nothing (a version not installed, an app not present)
    contributes nothing — an absent binary needs no rule and causes no popup.
    """
    seen: dict[str, Path] = {}
    machine = _machine_name()
    for entry in FIREWALL_MANIFEST:
        if entry.machine is not None and entry.machine != machine:
            continue
        for pattern in entry.paths:
            for hit in _expand_glob(pattern):
                if hit.name.endswith("-config"):
                    continue  # config shims never listen; nothing to allow
                seen[str(hit.resolve())] = hit
    return tuple(sorted(seen.values(), key=str))


def _manifest_rule_state(path: Path, rules: dict[str, bool]) -> str:
    """Render-facing ALF state for one resolved manifest path."""
    states = (rules.get(str(path)), rules.get(str(path.resolve())))
    if True in states:
        return "Allow"
    if False in states:
        return "Block"
    return "Missing"


def render_manifest_status(rules: dict[str, bool]) -> str:
    """Render every applicable manifest entry, pattern, resolved path, and state."""
    lines: list[str] = []
    resolved_states: dict[str, str] = {}
    unmatched_patterns = 0
    machine = _machine_name()
    entries = tuple(
        entry for entry in FIREWALL_MANIFEST if entry.machine is None or entry.machine == machine
    )
    for entry in entries:
        entry_lines, entry_states, entry_unmatched = _render_manifest_entry(entry, rules)
        lines.extend(entry_lines)
        resolved_states.update(entry_states)
        unmatched_patterns += entry_unmatched
    counts = {
        state: tuple(resolved_states.values()).count(state)
        for state in ("Allow", "Block", "Missing")
    }
    lines.append(
        f"  Summary: {len(entries)} entries; {len(resolved_states)} resolved paths "
        f"(Allow {counts['Allow']}, Block {counts['Block']}, Missing {counts['Missing']}); "
        f"{unmatched_patterns} patterns matched no installed binary"
    )
    return "\n".join(lines)


def _render_manifest_entry(
    entry: ManifestEntry, rules: dict[str, bool]
) -> tuple[list[str], dict[str, str], int]:
    lines = [f"  {entry.id} — {entry.purpose}"]
    resolved_states: dict[str, str] = {}
    unmatched_patterns = 0
    for pattern in entry.paths:
        lines.append(f"    pattern: {pattern}")
        hits = tuple(hit for hit in _expand_glob(pattern) if not hit.name.endswith("-config"))
        if not hits:
            unmatched_patterns += 1
            lines.append("      resolved: (no installed binary) [Missing]")
            continue
        for hit in hits:
            state = _manifest_rule_state(hit, rules)
            resolved_states[str(hit.resolve())] = state
            lines.append(f"      resolved: {hit} [{state}]")
    return lines, resolved_states, unmatched_patterns


def missing_allow_rules(paths: tuple[Path, ...], rules: dict[str, bool]) -> tuple[Path, ...]:
    """Of ``paths``, which have no Allow rule (or a Block rule) in ``rules``?"""
    return tuple(p for p in paths if not _covered(p, rules))


def stale_manifest_rules(rules: dict[str, bool]) -> tuple[Path, ...]:
    """ALF rules this manifest family manages whose path no longer exists.

    A version bump orphans the old rule: the rule still names the old path while
    the binary now runs from a new one (which the manifest re-adds). The orphan
    is harmless — nothing runs from it — but it accumulates with every upgrade.
    Only rules matching the manifest's own glob families (or the legacy family)
    are considered, so Apple's entries and unrelated apps are never touched.
    """
    family = [
        str(Path(pattern).expanduser()) for entry in FIREWALL_MANIFEST for pattern in entry.paths
    ]
    family.extend(str(Path(pattern).expanduser()) for pattern in FIREWALL_LEGACY_FAMILY)
    stale = (
        Path(path)
        for path in rules
        if not Path(path).exists() and any(Path(path).match(pattern) for pattern in family)
    )
    return tuple(sorted(stale, key=str))


_REPAIR_VERBS: tuple[str, ...] = ("--add", "--unblockapp")
_PRUNE_VERB = "--remove"


def _sudo_mutate(verb: str, path: str) -> bool:
    """Apply one ALF mutation directly, then through ``sudo -n`` if refused.

    The name is retained as the existing mutation seam. Both attempts are
    non-interactive and bounded; callers report exact manual commands if both
    platform paths fail.
    """
    commands = (
        [SOCKETFILTERFW, verb, path],
        ["sudo", "-n", SOCKETFILTERFW, verb, path],
    )
    for command in commands:
        try:
            proc = run_bounded(
                command,
                capture_output=True,
                text=True,
                timeout=_QUERY_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return True
    return False


def sudo_grant_installed() -> bool:
    """Is an older-macOS NOPASSWD fallback grant installed?

    Read-only probe: ``sudo -n -l`` lists the caller's sudoers privileges
    without a password when any NOPASSWD entry exists, and fails immediately
    otherwise. The grant is identified by its Cmnd_Alias name in the listing.
    """
    try:
        proc = subprocess.run(
            ["sudo", "-n", "-l"],
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return "AVA_FIREWALL" in proc.stdout


@dataclass(frozen=True)
class FirewallRepair:
    """Outcome of one repair pass: what changed, and what could not."""

    allowed: tuple[Path, ...] = ()
    failed: tuple[Path, ...] = ()
    removed: tuple[Path, ...] = ()


def repair_allowlist(paths: tuple[Path, ...], *, rules: dict[str, bool]) -> FirewallRepair:
    """Add Allow rules for every path in ``paths`` that lacks one.

    Mutating — direct first, as empirically verified on macOS 15.3.1, with
    ``sudo -n`` as a fallback on platforms that reject that path. ``--add``
    creates the rule; ``--unblockapp`` un-blocks a rule that exists in the Block
    state, which ``--add`` alone would leave blocking. Both verbs are idempotent,
    so re-running after a partial failure is safe. A path whose commands did not
    all succeed lands in ``failed`` for the caller to report.
    """
    missing = missing_allow_rules(paths, rules)
    allowed: list[Path] = []
    failed: list[Path] = []
    for path in missing:
        ok = all(_sudo_mutate(verb, str(path)) for verb in _REPAIR_VERBS)
        (allowed if ok else failed).append(path)
    return FirewallRepair(allowed=tuple(allowed), failed=tuple(failed))


def prune_stale_rules(rules: dict[str, bool]) -> FirewallRepair:
    """Remove stale manifest-family rules (see ``stale_manifest_rules``).

    Hygiene only — an orphaned rule blocks nothing and prompts nothing — so a
    caller should stay quiet about failures when the grant is absent.
    """
    removed: list[Path] = []
    for path in stale_manifest_rules(rules):
        if _sudo_mutate(_PRUNE_VERB, str(path)):
            removed.append(path)
    return FirewallRepair(removed=tuple(removed))
