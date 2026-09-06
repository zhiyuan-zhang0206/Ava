"""Gate converge step — record backfill + .env materialization, and the launchd
registration's idempotence.

The registration tests turn `os_jobs_enabled` back ON deliberately (the suite runs
with `AVA_OS_JOBS_ENABLED=false`) and drive a fake `launchctl`: the seam is
`_launchctl`, so no real job is ever booted out and `_plist_path` is redirected
into tmp so the operator's `~/Library/LaunchAgents` is never written.

What they pin is the 2026-08-01 outage in three parts — an unchanged gate is not
touched at all, a changed one is not bootstrapped until launchd has forgotten the
old job, and a bootstrap that never lands RAISES instead of logging and letting
the rollout report success over a dark entry port.

The content-hash tests pin the other side of the same rule: "unchanged" has to mean
the gate itself, so a rollout that only edits `services/gate/` still reaches the
running process instead of sitting on disk behind it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from cli.commands import _converge_gate as cg
from shared.cluster import ClusterPorts, ClusterRecord

# Captured at import, before the directory-wide `_gate_probe_offline` fixture stubs
# the module attribute — the handle the tests of the real dial use.
_REAL_ENTRY_ANSWERS = cg._entry_answers


def _write_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rec: ClusterRecord) -> None:
    reg_path = tmp_path / "clusters.json"
    reg_path.write_text(json.dumps({rec.gateway_home: rec.__dict__}))
    monkeypatch.setattr(cg.settings.general, "cluster_registry", reg_path)


def test_ensure_app_port_backfills_record_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "ava-dev"
    rec = ClusterRecord(
        ports=cast("ClusterPorts", {"gateway": 18032, "frontend": 18033}),
        gateway_home=str(home),
        created_at="t",
    )
    _write_registry(monkeypatch, tmp_path, rec)
    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("AVA_GATEWAY_PORT=18032\n")

    assert cg._ensure_app_port(home) == 18032 + 15

    # record gained the slot
    from shared.cluster import load_registry

    saved = load_registry()[str(home)]
    assert saved.ports.get("app") == 18032 + 15
    # .env gained the line
    assert "AVA_APP_PORT=18047" in env_path.read_text()


def test_ensure_app_port_keeps_existing_slot_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "ava-dev"
    rec = ClusterRecord(
        ports=cast("ClusterPorts", {"gateway": 18032, "frontend": 18033, "app": 18099}),
        gateway_home=str(home),
        created_at="t",
    )
    _write_registry(monkeypatch, tmp_path, rec)
    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("AVA_APP_PORT=18099\n")

    assert cg._ensure_app_port(home) == 18099
    from shared.cluster import load_registry

    assert load_registry()[str(home)].ports.get("app") == 18099
    assert "AVA_APP_PORT=18099" in env_path.read_text()


def test_ensure_app_port_raises_without_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cg.settings.general, "cluster_registry", tmp_path / "empty.json")
    with pytest.raises(RuntimeError, match="no registry record"):
        cg._ensure_app_port(tmp_path / "never-born")


# ── launchd registration: idempotence + the bootout/bootstrap race ─────────────


class _FakeLaunchctl:
    """A `launchctl` stand-in that models the one behaviour the fix is about.

    `bootout` does not make the job disappear at once — `linger` says how many
    subsequent `print` calls still report it loaded, which is the real teardown
    window a draining gate holds open. `bootstrap` fails while the job is still
    there (launchd's `Bootstrap failed: 5: Input/output error`), so a test that
    bootstrapped too early fails on the same error prod saw.
    """

    def __init__(self, *, loaded: bool, linger: int = 0, always_fail: bool = False) -> None:
        self.loaded = loaded
        self.linger = linger
        self.always_fail = always_fail
        self.verbs: list[str] = []
        self.bootstraps_while_loaded = 0

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        verb = args[0]
        self.verbs.append(verb)
        if verb == "print":
            if self.loaded and self.linger > 0:
                self.linger -= 1
                if self.linger == 0:
                    self.loaded = False
            return subprocess.CompletedProcess([], 0 if self.loaded else 1, "", "")
        if verb == "bootout":
            if self.linger == 0:
                self.loaded = False
            return subprocess.CompletedProcess([], 0, "", "")
        if verb == "bootstrap":
            if self.loaded:
                self.bootstraps_while_loaded += 1
            if self.always_fail or self.loaded:
                return subprocess.CompletedProcess(
                    [], 5, "", "Bootstrap failed: 5: Input/output error"
                )
            self.loaded = True
            return subprocess.CompletedProcess([], 0, "", "")
        raise AssertionError(f"unexpected launchctl verb: {verb}")


@pytest.fixture
def _launchd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[float]:
    """Arm the launchd path: OS jobs on, plist redirected into tmp, sleeps recorded."""
    monkeypatch.setattr(cg.settings.general, "os_jobs_enabled", True)
    monkeypatch.setattr(cg, "_plist_path", lambda _home: tmp_path / "gate.plist")  # pyright: ignore[reportUnknownArgumentType]
    slept: list[float] = []
    monkeypatch.setattr(cg, "_sleep", slept.append)
    return slept


def _home_and_repo(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "home", tmp_path / "repo"


def test_unchanged_plist_with_loaded_job_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _launchd: list[float]
) -> None:
    """The common rollout: same checkout, same ports. The gate process must be left
    alone — no bootout, no bootstrap, not even a rewrite of the plist (which is what
    makes "survives updates BY CONSTRUCTION" true rather than aspirational)."""
    home, repo = _home_and_repo(tmp_path)
    plist = cg._plist_path(home)
    plist.write_text(cg._plist_content(home, repo))
    before = plist.stat().st_mtime_ns
    fake = _FakeLaunchctl(loaded=True)
    monkeypatch.setattr(cg, "_launchctl", fake)

    cg._ensure_launchd(home, repo)

    assert "bootout" not in fake.verbs
    assert "bootstrap" not in fake.verbs
    assert plist.stat().st_mtime_ns == before  # not rewritten either
    assert _launchd == []  # nothing to wait for


def test_current_plist_but_job_not_loaded_is_reinstalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _launchd: list[float]
) -> None:
    """A plist that matches is not enough — a gate booted out by hand (or by a
    converge that died between bootout and bootstrap) has to be put back."""
    home, repo = _home_and_repo(tmp_path)
    cg._plist_path(home).write_text(cg._plist_content(home, repo))
    fake = _FakeLaunchctl(loaded=False)
    monkeypatch.setattr(cg, "_launchctl", fake)

    cg._ensure_launchd(home, repo)

    assert "bootstrap" in fake.verbs
    assert fake.loaded


def test_changed_plist_waits_for_teardown_before_bootstrapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _launchd: list[float]
) -> None:
    """The fix for the race: the job lingers for two polls after bootout, and the
    bootstrap is not issued until `launchctl print` says it is gone."""
    home, repo = _home_and_repo(tmp_path)
    plist = cg._plist_path(home)
    plist.write_text("<plist>stale — an older checkout path</plist>")
    fake = _FakeLaunchctl(loaded=True, linger=2)
    monkeypatch.setattr(cg, "_launchctl", fake)

    cg._ensure_launchd(home, repo)

    assert fake.bootstraps_while_loaded == 0  # never bootstrapped into a live job
    assert fake.verbs.index("bootout") < fake.verbs.index("bootstrap")
    assert _launchd == [cg._BOOTOUT_POLL_INTERVAL_S]  # polled while it drained
    assert plist.read_text() == cg._plist_content(home, repo)


def test_bootstrap_that_never_lands_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _launchd: list[float]
) -> None:
    """The silent half of the outage: converge logged the failed bootstrap and the
    rollout still reported success. Every attempt is spent, then it raises — converge
    is fail-fast, so `ava start` / the rollout fails instead of leaving :3000 dark."""
    home, repo = _home_and_repo(tmp_path)
    cg._plist_path(home).write_text("<plist>stale</plist>")
    fake = _FakeLaunchctl(loaded=True, always_fail=True)
    monkeypatch.setattr(cg, "_launchctl", fake)

    with pytest.raises(RuntimeError, match="would not load"):
        cg._ensure_launchd(home, repo)

    attempts = len(cg._BOOTSTRAP_BACKOFF_S) + 1
    assert fake.verbs.count("bootstrap") == attempts
    assert _launchd == list(cg._BOOTSTRAP_BACKOFF_S)  # backoff between retries only


def test_bootout_timeout_still_attempts_the_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _launchd: list[float]
) -> None:
    """A job that never disappears is not a reason to give up without trying: the
    wait is bounded and the bootstrap (with its own retries) is what decides."""
    home, repo = _home_and_repo(tmp_path)
    cg._plist_path(home).write_text("<plist>stale</plist>")
    monkeypatch.setattr(cg, "_BOOTOUT_TIMEOUT_S", 0.0)  # the deadline is already past
    fake = _FakeLaunchctl(loaded=True, linger=99)
    monkeypatch.setattr(cg, "_launchctl", fake)

    with pytest.raises(RuntimeError, match="would not load"):
        cg._ensure_launchd(home, repo)

    assert fake.verbs.count("bootstrap") == len(cg._BOOTSTRAP_BACKOFF_S) + 1


def test_os_jobs_disabled_skips_launchd_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suite-wide switch still wins — no plist read, no launchctl call."""
    home, repo = _home_and_repo(tmp_path)
    monkeypatch.setattr(cg.settings.general, "os_jobs_enabled", False)
    fake = _FakeLaunchctl(loaded=False)
    monkeypatch.setattr(cg, "_launchctl", fake)

    cg._ensure_launchd(home, repo)

    assert fake.verbs == []


# ── the gate's content hash: what makes "the plist changed" mean "the gate did" ──

_GATE_FILES = (
    ("daemon.py", "print('gate')\n"),
    ("static/login.html", "<h1>login</h1>"),
    ("static/theme.css", ":root { --border: #000; }"),
)


def _gate_tree(root: Path, *, reverse: bool = False) -> Path:
    """A miniature `services/gate/` — a daemon module plus static assets.

    `reverse` writes the same files in the opposite order: the hash has to be a
    function of the content, not of whatever order the filesystem hands the tree
    back, or the first converge after any unrelated write would swap the job.
    """
    for rel, text in reversed(_GATE_FILES) if reverse else _GATE_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_content_hash_is_a_function_of_content_alone(tmp_path: Path) -> None:
    """Same files, different write order, two different parent paths — one hash."""
    assert cg._gate_content_hash(_gate_tree(tmp_path / "a")) == cg._gate_content_hash(
        _gate_tree(tmp_path / "b", reverse=True)
    )


def test_content_hash_ignores_derived_files(tmp_path: Path) -> None:
    """Running the daemon once leaves a `__pycache__` next to its source. That is not
    a gate change, and if it read as one every converge after the first would boot the
    entry port out and back in for nothing."""
    tree = _gate_tree(tmp_path / "gate")
    before = cg._gate_content_hash(tree)

    pycache = tree / "__pycache__"
    pycache.mkdir()
    (pycache / "daemon.cpython-312.pyc").write_bytes(b"\x00compiled")

    assert cg._gate_content_hash(tree) == before


def test_content_hash_moves_for_assets_and_for_code(tmp_path: Path) -> None:
    """Both halves of the directory count. The static pages are the visible one, but
    `daemon.py` is read into the process at boot exactly like they are — a change to
    either is invisible until the job is replaced."""
    tree = _gate_tree(tmp_path / "gate")
    baseline = cg._gate_content_hash(tree)

    (tree / "static" / "login.html").write_text("<h1>rebuilt</h1>")
    after_asset = cg._gate_content_hash(tree)
    assert after_asset != baseline

    (tree / "daemon.py").write_text("print('gate v2')\n")
    assert cg._gate_content_hash(tree) != after_asset


def test_plist_carries_the_content_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The value has no reader — the daemon never looks at it — so the plist carrying
    it IS the whole mechanism, and that is what this asserts."""
    home, repo = _home_and_repo(tmp_path)
    source = _gate_tree(tmp_path / "gate-src")
    monkeypatch.setattr(cg, "_gate_source_dir", lambda: source)

    content = cg._plist_content(home, repo)

    assert "<key>AVA_GATE_CONTENT_HASH</key>" in content
    assert f"<string>{cg._gate_content_hash(source)}</string>" in content


def test_changed_gate_content_replaces_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _launchd: list[float]
) -> None:
    """The deployment gap this closes: a rollout that rebuilds the gate's pages moves
    neither the checkout path nor the ports, so before the hash the desired plist was
    byte-identical, converge correctly did nothing, and the old process kept serving
    the old pages until someone restarted it by hand."""
    home, repo = _home_and_repo(tmp_path)
    source = _gate_tree(tmp_path / "gate-src")
    monkeypatch.setattr(cg, "_gate_source_dir", lambda: source)
    plist = cg._plist_path(home)
    plist.write_text(cg._plist_content(home, repo))
    (source / "static" / "login.html").write_text("<h1>rebuilt on the app's tokens</h1>")
    fake = _FakeLaunchctl(loaded=True)
    monkeypatch.setattr(cg, "_launchctl", fake)

    cg._ensure_launchd(home, repo)

    # the ordinary replacement path, unchanged — not a second mechanism beside it
    assert fake.verbs.index("bootout") < fake.verbs.index("bootstrap")
    assert fake.bootstraps_while_loaded == 0
    assert cg._gate_content_hash(source) in plist.read_text()


def test_unchanged_gate_content_is_still_a_full_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _launchd: list[float]
) -> None:
    """The hash is a third input to "did the plist change", so the no-op case is
    re-asserted with the gate source pinned: a hash that wobbled between two calls on
    identical content would turn every converge into a swap, which is the 2026-08-01
    failure mode reintroduced from the other direction."""
    home, repo = _home_and_repo(tmp_path)
    source = _gate_tree(tmp_path / "gate-src")
    monkeypatch.setattr(cg, "_gate_source_dir", lambda: source)
    plist = cg._plist_path(home)
    plist.write_text(cg._plist_content(home, repo))
    before = plist.stat().st_mtime_ns
    fake = _FakeLaunchctl(loaded=True)
    monkeypatch.setattr(cg, "_launchctl", fake)

    cg._ensure_launchd(home, repo)

    assert "bootout" not in fake.verbs
    assert "bootstrap" not in fake.verbs
    assert plist.stat().st_mtime_ns == before


# ── gate observation (`ava status` / health-probe check 5) ─────────────────────


def test_probe_gate_counts_a_503_as_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_entry_answers` must accept ANY HTTP status. The gate answers 503 with the
    updating page for the whole of a rollout — a 2xx test would call it dead exactly
    when it is doing the job it exists for."""
    import httpx

    captured: dict[str, str] = {}

    def _fake_get(url: str, **_kw: object) -> httpx.Response:
        captured["url"] = url
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert _REAL_ENTRY_ANSWERS(3000) is True
    assert captured["url"] == "http://127.0.0.1:3000/"


def test_probe_gate_reports_a_refused_entry_as_down(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def _refuse(url: str, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _refuse)
    assert _REAL_ENTRY_ANSWERS(3000) is False


def test_print_gate_status_names_both_halves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dark entry and a missing supervisor are separate lines with separate
    remedies — the operator has to be able to tell a crashing gate from one that was
    never registered."""
    monkeypatch.setattr(
        cg,
        "probe_gate",
        lambda *_a: cg.GateStatus(3000, 3001, False, False, "launchd job com.x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    cg.print_gate_status()
    out = capsys.readouterr().out
    assert "✗ entry :3000 not answering" in out
    assert "gate.log" in out
    assert "✗ not supervised — launchd job com.x is absent" in out


def test_print_gate_status_healthy_shows_both_ports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cg,
        "probe_gate",
        lambda *_a: cg.GateStatus(3000, 3001, True, True, "launchd job com.x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    cg.print_gate_status()
    out = capsys.readouterr().out
    assert "✓ entry :3000 answering (proxies app :3001)" in out
    assert "✓ supervised by launchd job com.x" in out


# -- gate pid ownership (the detached, non-macOS supervisor) -------------------


def test_gate_pid_rejects_a_live_pid_that_is_not_our_daemon(tmp_path: Path) -> None:
    """A recycled pid must not read as the gate. This test's own interpreter is the
    stranger — really alive, definitively not `-m services.gate.daemon`."""
    import os

    home = tmp_path / "home"
    (home / "run").mkdir(parents=True)
    (home / "run" / "gate.pid").write_text(str(os.getpid()))

    assert cg.gate_pid(home, tmp_path / "repo") is None
    assert not (home / "run" / "gate.pid").exists(), "the stale pidfile must be cleared"


def test_gate_pid_rejects_a_daemon_from_another_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same daemon, different checkout: the cwd is what tells the two apart, and it
    is compared as a resolved path — one checkout path is routinely a prefix of
    another's (`/x/ava` vs `/x/ava-preview`)."""
    ours = tmp_path / "ava"
    theirs = tmp_path / "ava-preview"
    ours.mkdir()
    theirs.mkdir()

    class _Proc:
        def __init__(self, _pid: int) -> None: ...
        def cmdline(self) -> list[str]:
            return ["/x/.venv/bin/python", "-m", "services.gate.daemon"]

        def cwd(self) -> str:
            return str(theirs)

    import psutil

    monkeypatch.setattr(psutil, "Process", _Proc)
    assert cg.gate_pid_is_ours(4242, theirs) is True
    assert cg.gate_pid_is_ours(4242, ours) is False


def test_ensure_detached_launches_when_the_pidfile_is_a_recycled_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start-side half: a stale pidfile must not be read as "already running".

    Untreated, `_ensure_detached` returns early forever — and since only a launch
    rewrites the pidfile, the gate never comes back to its entry port."""
    import os

    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / "run").mkdir(parents=True)
    (home / "logs").mkdir(parents=True)
    repo.mkdir()
    (home / "run" / "gate.pid").write_text(str(os.getpid()))

    launched: list[list[str]] = []

    class _Spawned:
        pid = 31337

    def fake_popen(argv: list[str], **_kw: object) -> _Spawned:
        launched.append(argv)
        return _Spawned()

    monkeypatch.setattr(cg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cg, "_gate_python", lambda: "/x/.venv/bin/python")

    cg._ensure_detached(home, repo)

    assert launched and "services.gate.daemon" in launched[0]
    assert (home / "run" / "gate.pid").read_text() == "31337"


def test_linux_gate_requires_real_supervision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pidfile cannot provide the Linux gate's crash/restart contract."""
    monkeypatch.setattr(cg.sys, "platform", "linux")
    monkeypatch.setattr(cg, "_pidfile_alive", lambda _: True)  # pyright: ignore[reportUnknownArgumentType]
    status = cg.probe_gate(tmp_path)
    assert "systemd" in status.supervisor
    assert not status.supervised


@pytest.mark.parametrize(
    "argv",
    [["python", "-m", "services.gate.daemon_other"], ["sh", "-c", "echo services.gate.daemon"]],
)
def test_gate_pid_rejects_module_substrings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    import psutil

    class Proc:
        def __init__(self, _pid: int) -> None: ...
        def cmdline(self) -> list[str]:
            return argv

        def cwd(self) -> str:
            return str(tmp_path)

    monkeypatch.setattr(psutil, "Process", Proc)
    assert not cg.gate_pid_is_ours(4242, tmp_path)
