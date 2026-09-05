"""The macOS Application Firewall audit's decision logic, against stubbed queries.

## What these tests do and do not prove

They prove the *decision*: given a `--getglobalstate` / `--listapps` answer, which
verdict the audit reaches and which binaries it names. Mutation tests replace the
subprocess seam with a fake daemon that persists mutations, so no test changes
the host firewall.

**No test here mutates a real firewall.** Read and mutation subprocess seams are
stubbed. The repair tests pin the unprivileged-first path (exit 0 as the
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

from pathlib import Path

import pytest

import shared.macos_firewall as fw

# Real `--listapps` output from the macmini running macOS 15.3.1, trimmed to four
# entries with a Block state substituted into one. The exact two-line-per-rule
# shape (index line, then an indented parenthesised state) is what the parser
# keys on.
_LISTAPPS_OUTPUT = """Total number of apps = 4
1 : /usr/bin/python3
             (Allow incoming connections)
2 : /usr/sbin/smbd
             (Allow incoming connections)
3 : /opt/ava/bin/postgres
             (Block incoming connections)
4 : /usr/libexec/sharingd
             (Allow incoming connections)
"""


def _stub_query(monkeypatch: pytest.MonkeyPatch, *, state: str | None, apps: str | None) -> None:
    """Replace the single subprocess seam. None means "the query could not be made"."""

    def fake(*args: str) -> str | None:
        if args == ("--getglobalstate",):
            return state
        if args == ("--listapps",):
            return apps
        raise AssertionError(f"unexpected query: {args}")

    monkeypatch.setattr(fw, "_query", fake)


def _darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fw.platform, "system", lambda: "Darwin")


def _existing(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n")
    return p


# --- parsing ---------------------------------------------------------------


def test_listapps_parses_allow_and_block_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both states are captured, because they need different repairs.

    An absent rule needs `--add`; a rule in the Block state needs `--unblockapp`
    and would survive `--add` untouched. Collapsing them to a membership set would
    make the second case look healthy.
    """
    _stub_query(monkeypatch, state="State = 1", apps=_LISTAPPS_OUTPUT)
    assert fw.allowlisted_paths() == {
        "/usr/bin/python3": True,
        "/usr/sbin/smbd": True,
        "/opt/ava/bin/postgres": False,
        "/usr/libexec/sharingd": True,
    }


def test_listapps_drops_an_entry_with_no_state_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """An index line whose state line never arrives is not guessed at.

    Membership with an unknown state is the exact ambiguity `--getappblocked` was
    rejected for, so it is dropped and reported as missing rather than assumed OK.
    """
    truncated = "Total number of apps = 1\n1 : /opt/ava/bin/postgres\n"
    _stub_query(monkeypatch, state="State = 1", apps=truncated)
    assert fw.allowlisted_paths() == {}


def test_firewall_enabled_reads_the_state_number(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_query(monkeypatch, state="Firewall is enabled. (State = 1)", apps=None)
    assert fw.firewall_enabled() is True
    _stub_query(monkeypatch, state="Firewall is disabled. (State = 0)", apps=None)
    assert fw.firewall_enabled() is False
    _stub_query(monkeypatch, state=None, apps=None)
    assert fw.firewall_enabled() is None


# --- verdicts --------------------------------------------------------------


def test_non_macos_is_a_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No ALF exists to audit, and the queries are never even attempted."""
    monkeypatch.setattr(fw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        fw,
        "_query",
        lambda *_a: pytest.fail("queried socketfilterfw on a non-macOS host"),  # pyright: ignore[reportUnknownArgumentType]
    )
    audit = fw.audit_allowlist((_existing(tmp_path, "python3.12"),), machine_host="10.0.0.4")
    assert audit.verdict is fw.FirewallVerdict.NOT_MACOS
    assert not audit.needs_operator


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "  LocalHost  "])
def test_loopback_only_host_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str
) -> None:
    """A single box serves nothing off-box, and ALF does not filter loopback.

    Checked before the firewall state so a zero-config box is never nagged about a
    rule that would change nothing for it — case and surrounding space included,
    since these arrive from `.env` and a `machine_host` file.
    """
    _darwin(monkeypatch)
    _stub_query(monkeypatch, state="State = 1", apps="Total number of apps = 0\n")
    audit = fw.audit_allowlist((_existing(tmp_path, "python3.12"),), machine_host=host)
    assert audit.verdict is fw.FirewallVerdict.LOOPBACK_ONLY
    assert not audit.needs_operator


def test_firewall_off_rules_itself_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With ALF off nothing is filtered, so a missing rule is not a defect.

    This verdict is load-bearing beyond staying quiet: it is what lets the rollout's
    OFF_BOX_UNREACHABLE report say the firewall is *not* the cause and send the
    operator to the address configuration instead.
    """
    _darwin(monkeypatch)
    _stub_query(monkeypatch, state="State = 0", apps=_LISTAPPS_OUTPUT)
    audit = fw.audit_allowlist((_existing(tmp_path, "python3.12"),), machine_host="10.0.0.4")
    assert audit.verdict is fw.FirewallVerdict.FIREWALL_OFF
    assert not audit.needs_operator


def test_already_correct_is_a_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every required binary carries an Allow rule — the healthy converged state."""
    _darwin(monkeypatch)
    py = _existing(tmp_path, "python3.12")
    _stub_query(
        monkeypatch,
        state="State = 1",
        apps=f"Total number of apps = 1\n1 : {py.resolve()}\n             (Allow incoming connections)\n",
    )
    audit = fw.audit_allowlist((py,), machine_host="10.0.0.4")
    assert audit.verdict is fw.FirewallVerdict.ALLOWED
    assert audit.missing == ()


def test_missing_rule_names_the_binary_and_the_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The #949 state: firewall on, off-box host, no rule for the serving binary."""
    _darwin(monkeypatch)
    py = _existing(tmp_path, "python3.12")
    _stub_query(monkeypatch, state="State = 1", apps=_LISTAPPS_OUTPUT)
    audit = fw.audit_allowlist((py,), machine_host="10.0.0.4")
    assert audit.verdict is fw.FirewallVerdict.RULES_MISSING
    assert audit.missing == (py.resolve(),)
    assert audit.repair_commands() == (
        f'sudo "{fw.SOCKETFILTERFW}" --add "{py.resolve()}"',
        f'sudo "{fw.SOCKETFILTERFW}" --unblockapp "{py.resolve()}"',
    )


def test_a_blocked_rule_counts_as_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A rule that exists but says Block is broken, not present.

    `--add` alone would report success and change nothing, which is why the repair
    always emits `--unblockapp` too.
    """
    _darwin(monkeypatch)
    pg = _existing(tmp_path, "postgres")
    _stub_query(
        monkeypatch,
        state="State = 1",
        apps=f"Total number of apps = 1\n1 : {pg.resolve()}\n             (Block incoming connections)\n",
    )
    audit = fw.audit_allowlist((pg,), machine_host="10.0.0.4")
    assert audit.verdict is fw.FirewallVerdict.RULES_MISSING
    assert audit.missing == (pg.resolve(),)


def test_symlinked_interpreter_is_audited_by_its_real_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`.venv/bin/python` is a symlink; ALF sees the version-stamped target bind.

    The whole defect is that the target path moves on a `uv python` bump, so an
    audit that compared the stable symlink would report healthy forever.
    """
    _darwin(monkeypatch)
    real = _existing(tmp_path, "python3.12")
    link = tmp_path / "venv-python"
    link.symlink_to(real)
    _stub_query(
        monkeypatch,
        state="State = 1",
        apps=f"Total number of apps = 1\n1 : {real}\n             (Allow incoming connections)\n",
    )
    assert (
        fw.audit_allowlist((link,), machine_host="10.0.0.4").verdict is fw.FirewallVerdict.ALLOWED
    )
    # And the bump: the rule still names the old path, the interpreter moved.
    moved = _existing(tmp_path, "python3.14")
    link.unlink()
    link.symlink_to(moved)
    after = fw.audit_allowlist((link,), machine_host="10.0.0.4")
    assert after.verdict is fw.FirewallVerdict.RULES_MISSING
    assert after.missing == (moved.resolve(),)


def test_a_rule_on_the_symlink_path_also_counts_as_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rule naming the stable symlink is accepted, not flagged.

    Both forms occur in the wild: `$(brew --prefix pgbouncer)/bin/pgbouncer` is the
    stable `/opt/homebrew/opt/pgbouncer/…` symlink onto a versioned Cellar path, and
    that symlink form is exactly what this repo's own older runbook told operators
    to `--add`. Flagging a host whose rule was added that way would print a scary
    block on every `ava start` of a host that is fine — and a step operators learn
    to ignore cannot report the real occurrence either.
    """
    _darwin(monkeypatch)
    real = _existing(tmp_path, "redis-server-8.8.0")
    link = tmp_path / "redis-server"
    link.symlink_to(real)
    _stub_query(
        monkeypatch,
        state="State = 1",
        apps=f"Total number of apps = 1\n1 : {link}\n             (Allow incoming connections)\n",
    )
    assert (
        fw.audit_allowlist((link,), machine_host="10.0.0.4").verdict is fw.FirewallVerdict.ALLOWED
    )


def test_unreadable_firewall_is_not_reported_as_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A query that cannot be answered is its own verdict, not a clean bill of health.

    Collapsing it into ALLOWED would reproduce #949's signature — a check that
    reports fine while the host is blackholed.
    """
    _darwin(monkeypatch)
    _stub_query(monkeypatch, state="State = 1", apps=None)
    audit = fw.audit_allowlist((_existing(tmp_path, "python3.12"),), machine_host="10.0.0.4")
    assert audit.verdict is fw.FirewallVerdict.UNREADABLE
    assert not audit.needs_operator


# --- declarative manifest --------------------------------------------------


def _stub_manifest(monkeypatch: pytest.MonkeyPatch, entries: tuple) -> None:
    monkeypatch.setattr(fw, "FIREWALL_MANIFEST", entries)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(fw, "FIREWALL_LEGACY_FAMILY", ())
    monkeypatch.setattr(fw, "_machine_name", lambda: "testhost")


def test_manifest_paths_resolves_globs_and_drops_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Version-stamped families resolve to whatever is installed; an absent
    version contributes nothing (it needs no rule and causes no popup)."""
    (tmp_path / "pg" / "17.4.0" / "bin").mkdir(parents=True)
    (tmp_path / "pg" / "17.4.0" / "bin" / "postgres").write_text("#!/bin/sh\n")
    (tmp_path / "pg" / "17.5.0" / "bin").mkdir(parents=True)
    (tmp_path / "pg" / "17.5.0" / "bin" / "postgres").write_text("#!/bin/sh\n")
    (tmp_path / "pg" / "18.0.0" / "bin").mkdir(parents=True)  # no binary inside
    _stub_manifest(
        monkeypatch,
        (fw.ManifestEntry("pg", (f"{tmp_path}/pg/*/bin/postgres",), "Accept Postgres clients"),),
    )
    got = fw.manifest_paths()
    assert [p.name for p in got] == ["postgres", "postgres"]


def test_manifest_paths_filters_machine_scoped_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User-app entries apply only on the machine they name."""
    app = tmp_path / "app" / "bin" / "app"
    app.parent.mkdir(parents=True)
    app.write_text("#!/bin/sh\n")
    _stub_manifest(
        monkeypatch,
        (fw.ManifestEntry("app", (str(app),), "Accept app clients", machine="machine-1"),),
    )
    monkeypatch.setattr(fw, "_machine_name", lambda: "otherhost")
    assert fw.manifest_paths() == ()
    monkeypatch.setattr(fw, "_machine_name", lambda: "machine-1")
    assert fw.manifest_paths() == (app,)


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
    assert fw.manifest_paths() == (real,)


def test_observability_manifest_entries_resolve_installed_globs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Grafana/Loki/otelcol install under the stable Ava paths is discovered.

    Removing any of the three manifest entries would let that binary's next bind
    recreate the ALF prompt storm after an upgrade.
    """
    paths = (
        tmp_path / ".ava/lgtm/native/grafana-home/bin/grafana",
        tmp_path / ".ava/lgtm/native/bin/loki",
        tmp_path / ".ava/otel-collector/otelcol-contrib",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
    ids = {"grafana", "loki", "otel collector"}
    entries = tuple(entry for entry in fw.FIREWALL_MANIFEST if entry.id in ids)
    assert {entry.id for entry in entries} == ids
    monkeypatch.setenv("HOME", str(tmp_path))
    _stub_manifest(monkeypatch, entries)
    assert set(paths).issubset(fw.manifest_paths())


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    rendered = fw.render_manifest_status({str(allow): True, str(block): False})
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


# --- repair + prune (mutating — always through the stubbed bounded-process seam) -


class _FakeProc:
    def __init__(self, rc: int = 0, stdout: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout


def _render_apps(rules: dict[str, bool]) -> str:
    """Render a rules dict back into `--listapps` shape."""
    lines = [f"Total number of apps = {len(rules)}"]
    for i, (path, allow) in enumerate(sorted(rules.items()), 1):
        lines.append(f"{i} : {path}")
        lines.append(f"             ({'Allow' if allow else 'Block'} incoming connections)")
    return "\n".join(lines) + "\n"


def _stub_mutating_daemon(
    monkeypatch: pytest.MonkeyPatch,
    rules: dict[str, bool],
    *,
    direct_rc: int = 0,
    drop_adds: bool = False,
) -> list[list[str]]:
    """A fake ALF daemon that persists mutations, so repair's post-add
    verification has something to see.

    `direct_rc` simulates an older macOS that rejects the unprivileged mutation
    (the `sudo -n` fallback then does the work); `drop_adds=True` simulates the
    macOS 15.3.1 identifier-dedup no-op — every add exits 0 but the rule never
    appears in `--listapps`.
    """
    state = dict(rules)
    calls: list[list[str]] = []

    def fake_query(*args: str) -> str | None:
        if args == ("--getglobalstate",):
            return "Firewall is enabled. (State = 1)"
        if args == ("--listapps",):
            return _render_apps(state)
        raise AssertionError(f"unexpected query: {args}")

    def fake_run(cmd: list[str], **_kw: object) -> _FakeProc:
        calls.append(cmd)
        privileged = cmd[:2] == ["sudo", "-n"]
        verb, path = cmd[-2], cmd[-1]
        rc = 0 if privileged or direct_rc == 0 else 1
        if rc == 0 and not drop_adds:
            if verb in {"--add", "--unblockapp"}:
                state[path] = True
            elif verb == "--remove":
                state.pop(path, None)
        return _FakeProc(rc=rc)

    monkeypatch.setattr(fw, "_query", fake_query)
    monkeypatch.setattr(fw, "run_bounded", fake_run)
    monkeypatch.setattr(fw, "_VERIFY_RETRY_S", 0.0)
    return calls


def test_repair_allowlist_issues_both_verbs_per_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--add` creates the rule; `--unblockapp` clears a Block state that `--add`
    alone would leave. Both are idempotent, so re-running is safe."""
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")
    calls = _stub_mutating_daemon(monkeypatch, {})
    repair = fw.repair_allowlist((py,), rules={})
    assert repair.allowed == (py,)
    assert repair.failed == ()
    assert calls == [
        [fw.SOCKETFILTERFW, "--add", str(py)],
        [fw.SOCKETFILTERFW, "--unblockapp", str(py)],
    ]


def test_repair_falls_back_to_noninteractive_sudo_on_older_macos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    py = _existing(tmp_path, "python3.12")
    calls = _stub_mutating_daemon(monkeypatch, {}, direct_rc=1)
    repair = fw.repair_allowlist((py,), rules={})
    assert repair.allowed == (py,)
    assert repair.failed == ()
    assert calls == [
        [fw.SOCKETFILTERFW, "--add", str(py)],
        ["sudo", "-n", fw.SOCKETFILTERFW, "--add", str(py)],
        [fw.SOCKETFILTERFW, "--unblockapp", str(py)],
        ["sudo", "-n", fw.SOCKETFILTERFW, "--unblockapp", str(py)],
    ]


def test_repair_reports_partial_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A binary whose second verb failed is failed, not silently half-fixed."""
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")
    state: dict[str, bool] = {}

    def fake_query(*args: str) -> str | None:
        if args == ("--getglobalstate",):
            return "Firewall is enabled. (State = 1)"
        if args == ("--listapps",):
            return _render_apps(state)
        raise AssertionError(f"unexpected query: {args}")

    def fake(cmd: list[str], **kw: object) -> _FakeProc:
        if cmd[-2] == "--add":
            state[cmd[-1]] = True
            return _FakeProc()
        return _FakeProc(rc=1)

    monkeypatch.setattr(fw, "_query", fake_query)
    monkeypatch.setattr(fw, "run_bounded", fake)
    monkeypatch.setattr(fw, "_VERIFY_RETRY_S", 0.0)
    repair = fw.repair_allowlist((py,), rules={})
    assert repair.allowed == ()
    assert repair.failed == (py,)


def test_repair_reports_silent_noop_as_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS 15.3.1's daemon accepts an `--add` whose bundle identifier already
    has a rule and persists nothing. Exit 0 must not be reported as allowed."""
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")
    calls = _stub_mutating_daemon(monkeypatch, {}, drop_adds=True)
    repair = fw.repair_allowlist((py,), rules={})
    assert repair.allowed == ()
    assert repair.failed == (py,)
    assert calls == [
        [fw.SOCKETFILTERFW, "--add", str(py)],
        [fw.SOCKETFILTERFW, "--unblockapp", str(py)],
    ]


def test_repair_retries_verification_until_the_rule_appears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The daemon persists asynchronously; verification re-reads instead of
    giving up after the first read.

    The fake daemon's persisted view lags the mutation by two reads: the rule
    only shows up on the third `--listapps` read. A verification that read
    once and gave up would report this repair failed, so the passing
    assertions pin the retry loop as load-bearing.
    """
    py = tmp_path / "python3.12"
    py.write_text("#!/bin/sh\n")
    state: dict[str, bool] = {}
    reads = 0

    def fake_query(*args: str) -> str | None:
        nonlocal reads
        if args == ("--getglobalstate",):
            return "Firewall is enabled. (State = 1)"
        if args == ("--listapps",):
            reads += 1
            if reads >= 3:  # slow daemon: the persisted view catches up late
                return _render_apps(state)
            return "Total number of apps = 0\n"
        raise AssertionError(f"unexpected query: {args}")

    def fake(cmd: list[str], **_kw: object) -> _FakeProc:
        if cmd[-2] == "--add":
            state[cmd[-1]] = True
        return _FakeProc()

    monkeypatch.setattr(fw, "_query", fake_query)
    monkeypatch.setattr(fw, "run_bounded", fake)
    monkeypatch.setattr(fw, "_VERIFY_RETRY_S", 0.0)
    repair = fw.repair_allowlist((py,), rules={})
    assert repair.allowed == (py,)
    assert repair.failed == ()
    assert reads >= 3  # the retry loop is what made the repair succeed


def test_prune_stale_rules_removes_managed_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    calls: list[list[str]] = []
    monkeypatch.setattr(fw, "run_bounded", lambda _cmd, **_kw: calls.append(_cmd) or _FakeProc())  # pyright: ignore[reportUnknownArgumentType]
    rules = {"/opt/homebrew/Cellar/node/25.6.1/bin/node": True}
    repair = fw.prune_stale_rules(rules)
    assert repair.removed == (Path("/opt/homebrew/Cellar/node/25.6.1/bin/node"),)
    assert calls == [[fw.SOCKETFILTERFW, "--remove", "/opt/homebrew/Cellar/node/25.6.1/bin/node"]]


def test_sudo_grant_probe_sees_the_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sudo -n -l` lists the Cmnd_Alias without a password when the grant exists."""
    monkeypatch.setattr(
        fw.subprocess,
        "run",
        lambda _cmd, **_kw: _FakeProc(  # pyright: ignore[reportUnknownArgumentType]
            stdout="User ava may run the following commands on this host:\n"
            "    (root) NOPASSWD: AVA_FIREWALL\n"
        ),
    )
    assert fw.sudo_grant_installed() is True


def test_sudo_grant_probe_absent_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fw.subprocess, "run", lambda _cmd, **_kw: _FakeProc(rc=1))  # pyright: ignore[reportUnknownArgumentType]
    assert fw.sudo_grant_installed() is False
