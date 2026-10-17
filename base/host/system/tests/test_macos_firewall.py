"""The macOS Application Firewall audit's decision logic, against scripted `socketfilterfw` answers.

## What these tests do and do not prove

They prove the *decision*: given a `--getglobalstate` / `--listapps` answer, which
verdict the audit reaches and which binaries it names. Mutation tests run against a
fake daemon that persists mutations, so no test changes the host firewall.

**No test here mutates a real firewall.** Every `socketfilterfw` and `sudo` call goes
through the `Host` a test builds. The repair tests pin the unprivileged-first path (exit 0 as the
ordinary user, re-verified on the macmini running macOS 15.3.1), the `sudo -n`
fallback for versions that still require it, and — the regression this suite
guards — the post-mutation verification: macOS 15.3.1's daemon *accepts* an
`--add` whose bundle identifier already has a rule and persists nothing, so a
repair that trusted the exit code would report "allowed" for rules that never
existed.

One thing *was* verified against the real tool during development, on the
macmini running macOS 15.3.1: `allowlisted_paths()` parsed that host's genuine
`--listapps` output into exactly the 8 rules the tool printed, and correctly
found the running uv interpreter absent from them. `_LISTAPPS_OUTPUT` below is
that real output, trimmed — so the parser is pinned against a true sample rather
than an invented one. A live `RULES_MISSING` (firewall on, off-box host) was
also observed on that host in August 2026, including the daemon's silent
no-op on identifier-colliding adds.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import base.host.system.macos_firewall as fw
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)

# Real `--listapps` output from the macmini running macOS 15.3.1, trimmed to four
# entries with a Block state substituted into one. The exact two-line-per-rule
# shape (index line, then an indented parenthesised state) is what the parser
# keys on.
_LISTAPPS_OUTPUT = """Total number of apps = 4
1 : /usr/bin/python3
             (Allow incoming connections)
2 : /usr/sbin/smbd
             (Allow incoming connections)
3 : /opt/meiva/bin/postgres
             (Block incoming connections)
4 : /usr/libexec/sharingd
             (Allow incoming connections)
"""

_TOOLS = {fw.SOCKETFILTERFW: fw.SOCKETFILTERFW}


def _alf(state: str | None, apps: str | None) -> Host:
    """A macOS host whose `socketfilterfw` answers as given; None means it could not answer."""
    commands = FakeCommands()
    commands.on(
        [fw.SOCKETFILTERFW, "--getglobalstate"],
        Reply(state or "", returncode=0 if state is not None else 1),
        repeat=True,
    )
    commands.on(
        [fw.SOCKETFILTERFW, "--listapps"],
        Reply(apps or "", returncode=0 if apps is not None else 1),
        repeat=True,
    )
    return fake_host(commands, platform=DARWIN_ARM64, programs=_TOOLS)


def _existing(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n")
    return p


# --- parsing ---------------------------------------------------------------


def test_listapps_parses_allow_and_block_states() -> None:
    """Both states are captured, because they need different repairs.

    An absent rule needs `--add`; a rule in the Block state needs `--unblockapp`
    and would survive `--add` untouched. Collapsing them to a membership set would
    make the second case look healthy.
    """
    host = _alf("State = 1", _LISTAPPS_OUTPUT)
    assert fw.allowlisted_paths(host=host) == {
        "/usr/bin/python3": True,
        "/usr/sbin/smbd": True,
        "/opt/meiva/bin/postgres": False,
        "/usr/libexec/sharingd": True,
    }


def test_listapps_drops_an_entry_with_no_state_line() -> None:
    """An index line whose state line never arrives is not guessed at.

    Membership with an unknown state is the exact ambiguity `--getappblocked` was
    rejected for, so it is dropped and reported as missing rather than assumed OK.
    """
    truncated = "Total number of apps = 1\n1 : /opt/meiva/bin/postgres\n"
    assert fw.allowlisted_paths(host=_alf("State = 1", truncated)) == {}


def test_firewall_enabled_reads_the_state_number() -> None:
    enabled = _alf("Firewall is enabled. (State = 1)", None)
    assert fw.firewall_enabled(host=enabled) is True
    disabled = _alf("Firewall is disabled. (State = 0)", None)
    assert fw.firewall_enabled(host=disabled) is False
    assert fw.firewall_enabled(host=_alf(None, None)) is None


# --- verdicts --------------------------------------------------------------


def test_non_macos_is_a_no_op(tmp_path: Path) -> None:
    """No ALF exists to audit, and the queries are never even attempted."""
    commands = FakeCommands()  # nothing scripted: a query would fail the test
    audit = fw.audit_allowlist(
        (_existing(tmp_path, "python3.12"),),
        machine_host="10.0.0.4",
        host=fake_host(commands, platform=LINUX_X86, programs=_TOOLS),
    )
    assert audit.verdict is fw.FirewallVerdict.NOT_MACOS
    assert not audit.needs_operator
    assert commands.argvs == []


@pytest.mark.parametrize("machine_host", ["localhost", "127.0.0.1", "::1", "  LocalHost  "])
def test_loopback_only_host_is_a_no_op(tmp_path: Path, machine_host: str) -> None:
    """A single box serves nothing off-box, and ALF does not filter loopback.

    Checked before the firewall state so a zero-config box is never nagged about a
    rule that would change nothing for it — case and surrounding space included,
    since these arrive from `.env`.
    """
    host = _alf("State = 1", "Total number of apps = 0\n")
    audit = fw.audit_allowlist(
        (_existing(tmp_path, "python3.12"),), machine_host=machine_host, host=host
    )
    assert audit.verdict is fw.FirewallVerdict.LOOPBACK_ONLY
    assert not audit.needs_operator


def test_firewall_off_rules_itself_out(tmp_path: Path) -> None:
    """With ALF off nothing is filtered, so a missing rule is not a defect.

    This verdict is load-bearing beyond staying quiet: it is what lets an off-box
    reachability diagnosis say the firewall is *not* the cause and send the
    operator to the address configuration instead.
    """
    host = _alf("State = 0", _LISTAPPS_OUTPUT)
    audit = fw.audit_allowlist(
        (_existing(tmp_path, "python3.12"),), machine_host="10.0.0.4", host=host
    )
    assert audit.verdict is fw.FirewallVerdict.FIREWALL_OFF
    assert not audit.needs_operator


def test_already_correct_is_a_no_op(tmp_path: Path) -> None:
    """Every required binary carries an Allow rule — the healthy converged state."""
    py = _existing(tmp_path, "python3.12")
    host = _alf(
        "State = 1",
        f"Total number of apps = 1\n1 : {py.resolve()}\n             (Allow incoming connections)\n",
    )
    audit = fw.audit_allowlist((py,), machine_host="10.0.0.4", host=host)
    assert audit.verdict is fw.FirewallVerdict.ALLOWED
    assert audit.missing == ()


def test_missing_rule_names_the_binary_and_the_repair(tmp_path: Path) -> None:
    """The #949 state: firewall on, off-box host, no rule for the serving binary."""
    py = _existing(tmp_path, "python3.12")
    host = _alf("State = 1", _LISTAPPS_OUTPUT)
    audit = fw.audit_allowlist((py,), machine_host="10.0.0.4", host=host)
    assert audit.verdict is fw.FirewallVerdict.RULES_MISSING
    assert audit.missing == (py.resolve(),)
    assert audit.repair_commands() == (
        f'sudo "{fw.SOCKETFILTERFW}" --add "{py.resolve()}"',
        f'sudo "{fw.SOCKETFILTERFW}" --unblockapp "{py.resolve()}"',
    )


def test_a_blocked_rule_counts_as_missing(tmp_path: Path) -> None:
    """A rule that exists but says Block is broken, not present.

    `--add` alone would report success and change nothing, which is why the repair
    always emits `--unblockapp` too.
    """
    pg = _existing(tmp_path, "postgres")
    host = _alf(
        "State = 1",
        f"Total number of apps = 1\n1 : {pg.resolve()}\n             (Block incoming connections)\n",
    )
    audit = fw.audit_allowlist((pg,), machine_host="10.0.0.4", host=host)
    assert audit.verdict is fw.FirewallVerdict.RULES_MISSING
    assert audit.missing == (pg.resolve(),)


def test_symlinked_interpreter_is_audited_by_its_real_path(tmp_path: Path) -> None:
    """`.venv/bin/python` is a symlink; ALF sees the version-stamped target bind.

    The whole defect is that the target path moves on a `uv python` bump, so an
    audit that compared the stable symlink would report healthy forever.
    """
    real = _existing(tmp_path, "python3.12")
    link = tmp_path / "venv-python"
    link.symlink_to(real)
    host = _alf(
        "State = 1",
        f"Total number of apps = 1\n1 : {real}\n             (Allow incoming connections)\n",
    )
    assert (
        fw.audit_allowlist((link,), machine_host="10.0.0.4", host=host).verdict
        is fw.FirewallVerdict.ALLOWED
    )
    # And the bump: the rule still names the old path, the interpreter moved.
    moved = _existing(tmp_path, "python3.14")
    link.unlink()
    link.symlink_to(moved)
    after = fw.audit_allowlist((link,), machine_host="10.0.0.4", host=host)
    assert after.verdict is fw.FirewallVerdict.RULES_MISSING
    assert after.missing == (moved.resolve(),)


def test_a_rule_on_the_symlink_path_also_counts_as_covered(tmp_path: Path) -> None:
    """A rule naming the stable symlink is accepted, not flagged.

    Both forms occur in the wild: `$(brew --prefix pgbouncer)/bin/pgbouncer` is the
    stable `/opt/homebrew/opt/pgbouncer/…` symlink onto a versioned Cellar path, and
    that symlink form is exactly what this repo's own older runbook told operators
    to `--add`. Flagging a host whose rule was added that way would print a scary
    block on every `meiva start` of a host that is fine — and a step operators learn
    to ignore cannot report the real occurrence either.
    """
    real = _existing(tmp_path, "redis-server-8.8.0")
    link = tmp_path / "redis-server"
    link.symlink_to(real)
    host = _alf(
        "State = 1",
        f"Total number of apps = 1\n1 : {link}\n             (Allow incoming connections)\n",
    )
    assert (
        fw.audit_allowlist((link,), machine_host="10.0.0.4", host=host).verdict
        is fw.FirewallVerdict.ALLOWED
    )


def test_unreadable_firewall_is_not_reported_as_healthy(tmp_path: Path) -> None:
    """A query that cannot be answered is its own verdict, not a clean bill of health.

    Collapsing it into ALLOWED would reproduce #949's signature — a check that
    reports fine while the host is blackholed.
    """
    host = _alf("State = 1", None)
    audit = fw.audit_allowlist(
        (_existing(tmp_path, "python3.12"),), machine_host="10.0.0.4", host=host
    )
    assert audit.verdict is fw.FirewallVerdict.UNREADABLE
    assert not audit.needs_operator


# --- declarative manifest --------------------------------------------------


def _stub_manifest(monkeypatch: pytest.MonkeyPatch, entries: tuple) -> None:
    monkeypatch.setattr(fw, "FIREWALL_MANIFEST", entries)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(fw, "FIREWALL_LEGACY_FAMILY", ())


def test_manifest_paths_resolves_globs_and_drops_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Version-stamped families resolve to whatever is installed; an absent
    version contributes nothing (it needs no rule and causes no popup)."""
    (tmp_path / "pg" / "17.11.0" / "bin").mkdir(parents=True)
    (tmp_path / "pg" / "17.11.0" / "bin" / "postgres").write_text("#!/bin/sh\n")
    (tmp_path / "pg" / "17.5.0" / "bin").mkdir(parents=True)
    (tmp_path / "pg" / "17.5.0" / "bin" / "postgres").write_text("#!/bin/sh\n")
    (tmp_path / "pg" / "18.0.0" / "bin").mkdir(parents=True)  # no binary inside
    _stub_manifest(
        monkeypatch,
        (fw.ManifestEntry("pg", (f"{tmp_path}/pg/*/bin/postgres",), "Accept Postgres clients"),),
    )
    got = fw.manifest_paths(machine_name="test-machine", host=fake_host())
    assert [p.name for p in got] == ["postgres", "postgres"]


def test_manifest_paths_filters_machine_scoped_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """User-app entries apply only on the machine they name."""
    app = tmp_path / "app" / "bin" / "app"
    app.parent.mkdir(parents=True)
    app.write_text("#!/bin/sh\n")
    _stub_manifest(
        monkeypatch,
        (fw.ManifestEntry("app", (str(app),), "Accept app clients", machine="machine-1"),),
    )
    assert fw.manifest_paths(machine_name="otherhost", host=fake_host()) == ()
    assert fw.manifest_paths(machine_name="machine-1", host=fake_host()) == (app,)


def test_manifest_paths_skips_config_shims(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`python3.12-config` matches the `python3.*` glob but never listens."""
    (tmp_path / "bin").mkdir()
    real = tmp_path / "bin" / "python3.12"
    real.write_text("#!/bin/sh\n")
    (tmp_path / "bin" / "python3.12-config").write_text("#!/bin/sh\n")
    _stub_manifest(
        monkeypatch,
        (fw.ManifestEntry("py", (f"{tmp_path}/bin/python3.*",), "Run Python listeners"),),
    )
    assert fw.manifest_paths(machine_name="test-machine", host=fake_host()) == (real,)


def test_observability_manifest_entries_resolve_installed_globs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Grafana/Loki/otelcol install under the stable Meiva paths is discovered.

    Removing any of the three manifest entries would let that binary's next bind
    recreate the ALF prompt storm after an upgrade.
    """
    paths = (
        tmp_path / ".meiva/lgtm/native/grafana-home/bin/grafana",
        tmp_path / ".meiva/lgtm/native/bin/loki",
        tmp_path / ".meiva/otel-collector/otelcol-contrib",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
    ids = {"grafana", "loki", "otel collector"}
    entries = tuple(entry for entry in fw.FIREWALL_MANIFEST if entry.id in ids)
    assert {entry.id for entry in entries} == ids
    monkeypatch.setenv("HOME", str(tmp_path))
    _stub_manifest(monkeypatch, entries)
    assert set(paths).issubset(fw.manifest_paths(machine_name="test-machine", host=fake_host()))


def test_stale_manifest_rules_only_touches_managed_absent_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pruning retires a versioned path that moved on, but never a live path or
    an absent path outside the manifest family (Apple's own entries)."""
    live = tmp_path / "node"
    live.write_text("#!/bin/sh\n")
    rules = {
        str(live): True,
        "/opt/homebrew/Cellar/node/25.6.1/bin/node": True,  # stale, managed
        "/usr/sbin/syslogd": True,  # absent but unmanaged — Apple's
    }
    _stub_manifest(
        monkeypatch,
        (
            fw.ManifestEntry(
                "node", ("/opt/homebrew/Cellar/node/*/bin/node",), "Serve frontend traffic"
            ),
        ),
    )
    assert fw.stale_manifest_rules(rules) == (Path("/opt/homebrew/Cellar/node/25.6.1/bin/node"),)


def test_stale_manifest_rules_covers_legacy_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The moved Android-SDK adb is retired by the legacy family glob."""
    monkeypatch.setattr(fw, "FIREWALL_MANIFEST", ())
    stale_path = tmp_path / "retired-android-sdk/platform-tools/adb"
    monkeypatch.setattr(fw, "FIREWALL_LEGACY_FAMILY", (str(stale_path),))
    rules = {str(stale_path): True}
    assert fw.stale_manifest_rules(rules) == (stale_path,)


def test_render_manifest_status_shows_entry_patterns_paths_and_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    allow = _existing(tmp_path, "allowed")
    block = _existing(tmp_path, "blocked")
    missing = _existing(tmp_path, "missing")
    absent_pattern = str(tmp_path / "not-installed/*/daemon")
    _stub_manifest(
        monkeypatch,
        (
            fw.ManifestEntry(
                "listeners",
                (str(allow), str(block), str(missing), absent_pattern),
                "Accept inbound test traffic",
            ),
        ),
    )
    rendered = fw.render_manifest_status(
        {str(allow): True, str(block): False}, machine_name="test-machine", host=fake_host()
    )
    assert "listeners — Accept inbound test traffic" in rendered
    assert f"pattern: {allow}" in rendered
    assert f"resolved: {allow} [Allow]" in rendered
    assert f"resolved: {block} [Block]" in rendered
    assert f"resolved: {missing} [Missing]" in rendered
    assert f"pattern: {absent_pattern}" in rendered
    assert "resolved: (no installed binary)" in rendered
    assert "Summary: 1 entries; 3 resolved paths (Allow 1, Block 1, Missing 1)" in rendered


def test_missing_allow_rules_ignores_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")
    rules = {str(py): True}
    assert fw.missing_allow_rules((py,), rules) == ()
    rules = {str(py): False}  # Block state counts as missing
    assert fw.missing_allow_rules((py,), rules) == (py,)


# --- repair + prune (mutating — always against the fake daemon below) -------


def _render_apps(rules: dict[str, bool]) -> str:
    """Render a rules dict back into `--listapps` shape."""
    lines = [f"Total number of apps = {len(rules)}"]
    for i, (path, allow) in enumerate(sorted(rules.items()), 1):
        lines.append(f"{i} : {path}")
        lines.append(f"             ({'Allow' if allow else 'Block'} incoming connections)")
    return "\n".join(lines) + "\n"


class _FakeAlf:
    """A fake ALF daemon behind `socketfilterfw` and `sudo -n socketfilterfw`, as a `CommandRunner`.

    It persists mutations, so repair's post-add verification has something to see.
    `direct_rc` simulates an older macOS that rejects the unprivileged mutation (the `sudo -n`
    fallback then does the work); `drop_adds=True` simulates the macOS 15.3.1 identifier-dedup
    no-op (every add exits 0 but the rule never appears in `--listapps`); `lag_reads` makes the
    persisted view catch up only after that many `--listapps` reads.
    """

    def __init__(
        self,
        rules: dict[str, bool],
        *,
        direct_rc: int = 0,
        drop_adds: bool = False,
        lag_reads: int = 0,
    ) -> None:
        self.state = dict(rules)
        self.direct_rc = direct_rc
        self.drop_adds = drop_adds
        self.lag_reads = lag_reads
        self.reads = 0
        self.mutations: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        **_popen_kwargs: object,
    ) -> subprocess.CompletedProcess[Any]:
        cmd = list(argv)
        answer = self._answer(cmd)
        return subprocess.CompletedProcess(cmd, answer[0], answer[1], "")

    def _answer(self, cmd: list[str]) -> tuple[int, str]:
        privileged = cmd[:2] == ["sudo", "-n"]
        tool = cmd[2:] if privileged else cmd
        if tool[0] != fw.SOCKETFILTERFW:
            raise AssertionError(f"unexpected command: {cmd}")
        if tool[1:] == ["--getglobalstate"]:
            return 0, "Firewall is enabled. (State = 1)"
        if tool[1:] == ["--listapps"]:
            self.reads += 1
            return 0, _render_apps(self.state) if self.reads > self.lag_reads else _render_apps({})
        verb, path = tool[1], tool[2]
        self.mutations.append(cmd)
        rc = 0 if privileged or self.direct_rc == 0 else 1
        if rc == 0 and not self.drop_adds:
            if verb in {"--add", "--unblockapp"}:
                self.state[path] = True
            elif verb == "--remove":
                self.state.pop(path, None)
        return rc, ""


def _alf_daemon(daemon: _FakeAlf) -> Host:
    return fake_host(daemon, platform=DARWIN_ARM64, programs=_TOOLS)


@pytest.fixture(autouse=True)
def _no_verification_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fw, "VERIFY_RETRY_S", 0.0)


def test_repair_allowlist_issues_both_verbs_per_binary(tmp_path: Path) -> None:
    """`--add` creates the rule; `--unblockapp` clears a Block state that `--add`
    alone would leave. Both are idempotent, so re-running is safe."""
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")
    daemon = _FakeAlf({})
    repair = fw.repair_allowlist((py,), rules={}, host=_alf_daemon(daemon))
    assert repair.allowed == (py,)
    assert repair.failed == ()
    assert daemon.mutations == [
        [fw.SOCKETFILTERFW, "--add", str(py)],
        [fw.SOCKETFILTERFW, "--unblockapp", str(py)],
    ]


def test_repair_falls_back_to_noninteractive_sudo_on_older_macos(tmp_path: Path) -> None:
    py = _existing(tmp_path, "python3.12")
    daemon = _FakeAlf({}, direct_rc=1)
    repair = fw.repair_allowlist((py,), rules={}, host=_alf_daemon(daemon))
    assert repair.allowed == (py,)
    assert repair.failed == ()
    assert daemon.mutations == [
        [fw.SOCKETFILTERFW, "--add", str(py)],
        ["sudo", "-n", fw.SOCKETFILTERFW, "--add", str(py)],
        [fw.SOCKETFILTERFW, "--unblockapp", str(py)],
        ["sudo", "-n", fw.SOCKETFILTERFW, "--unblockapp", str(py)],
    ]


def test_repair_reports_partial_failure(tmp_path: Path) -> None:
    """A binary whose second verb failed is failed, not silently half-fixed."""
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")

    class _RefusesUnblock(_FakeAlf):
        def _answer(self, cmd: list[str]) -> tuple[int, str]:
            if "--unblockapp" in cmd:
                return 1, ""
            return super()._answer(cmd)

    repair = fw.repair_allowlist((py,), rules={}, host=_alf_daemon(_RefusesUnblock({})))
    assert repair.allowed == ()
    assert repair.failed == (py,)


def test_repair_reports_silent_noop_as_failed(tmp_path: Path) -> None:
    """macOS 15.3.1's daemon accepts an `--add` whose bundle identifier already
    has a rule and persists nothing. Exit 0 must not be reported as allowed."""
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")
    daemon = _FakeAlf({}, drop_adds=True)
    repair = fw.repair_allowlist((py,), rules={}, host=_alf_daemon(daemon))
    assert repair.allowed == ()
    assert repair.failed == (py,)
    assert daemon.mutations == [
        [fw.SOCKETFILTERFW, "--add", str(py)],
        [fw.SOCKETFILTERFW, "--unblockapp", str(py)],
    ]


def test_repair_retries_verification_until_the_rule_appears(tmp_path: Path) -> None:
    """The daemon persists asynchronously; verification re-reads instead of
    giving up after the first read.

    The fake daemon's persisted view lags the mutation by two reads: the rule
    only shows up on the third `--listapps` read. A verification that read
    once and gave up would report this repair failed, so the passing
    assertions pin the retry loop as load-bearing.
    """
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")
    daemon = _FakeAlf({}, lag_reads=2)
    repair = fw.repair_allowlist((py,), rules={}, host=_alf_daemon(daemon))
    assert repair.allowed == (py,)
    assert repair.failed == ()
    assert daemon.reads >= 3  # the retry loop is what made the repair succeed


def test_prune_stale_rules_removes_managed_orphans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fw,
        "FIREWALL_MANIFEST",
        (
            fw.ManifestEntry(
                "node", ("/opt/homebrew/Cellar/node/*/bin/node",), "Serve frontend traffic"
            ),
        ),
    )
    monkeypatch.setattr(fw, "FIREWALL_LEGACY_FAMILY", ())
    stale = "/opt/homebrew/Cellar/node/25.6.1/bin/node"
    daemon = _FakeAlf({stale: True})
    repair = fw.prune_stale_rules({stale: True}, host=_alf_daemon(daemon))
    assert repair.removed == (Path(stale),)
    assert daemon.mutations == [[fw.SOCKETFILTERFW, "--remove", stale]]
    assert daemon.state == {}


def test_sudo_grant_probe_sees_the_alias() -> None:
    """`sudo -n -l` lists the Cmnd_Alias without a password when the grant exists."""
    commands = FakeCommands()
    commands.on(
        ["sudo", "-n", "-l"],
        Reply(
            "User meiva may run the following commands on this host:\n"
            "    (root) NOPASSWD: MEIVA_FIREWALL\n"
        ),
    )
    assert fw.sudo_grant_installed(host=fake_host(commands)) is True


def test_sudo_grant_probe_absent_fails_fast() -> None:
    commands = FakeCommands()
    commands.on(["sudo", "-n", "-l"], Reply(returncode=1))
    assert fw.sudo_grant_installed(host=fake_host(commands)) is False
