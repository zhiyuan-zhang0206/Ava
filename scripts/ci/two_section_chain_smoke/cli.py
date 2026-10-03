"""Two-section chain smoke: launchd -> permissions-helper -> ava-root -> unit."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import re
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, cast

from .f12 import _f12_finish, _f12_point
from .reconcile import _reconcile_phase
from .support import (
    _ROOT_SOCKET,
    _ROOT_WAIT_S,
    _UNIT_IDS,
    _UNIT_PROBE,
    SmokeError,
    _bootout,
    _fail,
    _job_pid,
    _kill,
    _launchctl_domain,
    _pid_alive,
    _ping_or_none,
    _ppid_of,
    _probe_pids,
    _ps_line,
    _refusal,
    _relaunched_helper,
    _root_call,
    _run,
    _save,
    _status_if_conflict,
    _status_if_keeper_running,
    _status_if_refused,
    _status_if_running,
    _tail,
    _unit_entry,
    _wait_for,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LABEL = "com.ava.test.two-section-chain-smoke"
BUNDLE_ID = "com.ava.permissions-helper"

_HELPER_WAIT_S = 30.0
_RESTART_WAIT_S = 20.0
_F12_STABLE_S = 6.0

_ATTRIBUTION_RE = re.compile(
    r"responsible=\{TCCDProcess: identifier=(?P<responsible_id>[^,]*), pid=(?P<responsible_pid>\d+)"
    r".*?(?P<role>requesting|accessing)=\{TCCDProcess: identifier=(?P<requesting_id>[^,]*), "
    r"pid=(?P<requesting_pid>\d+)"
)


def main() -> int:  # noqa: PLR0915 - one bounded smoke lifecycle: every phase, wait, and teardown live together on purpose
    parser = argparse.ArgumentParser(
        prog="two_section_chain_smoke", description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument(
        "--workdir",
        default="/tmp/two-section-chain-smoke",  # noqa: S108 - scratch evidence dir, never secret
    )
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--python", default=sys.executable, help="interpreter for root + units")
    parser.add_argument("--skip-attribution", action="store_true", help="skip the F11 tccd check")
    parser.add_argument(
        "--sample-restart",
        action="store_true",
        help="sample chains + TCC attribution across the root crash restart (F12)",
    )
    parser.add_argument(
        "--sample-conflict",
        action="store_true",
        help="sample chains + TCC attribution across the helper crash conflict phase (F12b)",
    )
    parser.add_argument(
        "--reconcile-case",
        action="store_true",
        help="append the cold-start reconcile case (dead generation -> release, no force)",
    )
    parser.add_argument("--cleanup", action="store_true", help="remove the workdir at the end")
    args = parser.parse_args()

    workdir = Path(args.workdir).expanduser()
    if not workdir.is_absolute():
        print(f"FAIL(phase=args): workdir must be absolute: {workdir}")
        return 2
    if os.getuid() == 0:
        print("FAIL(phase=args): run as the desktop user, not root (launchctl gui domain)")
        return 2
    if not (REPO_ROOT / "services" / "ava_root").is_dir():
        print(
            f"FAIL(phase=args): {REPO_ROOT} does not contain services/ava_root (stack the root slice)"
        )
        return 2

    workdir.mkdir(parents=True, exist_ok=True)
    evidence = workdir / "evidence"
    evidence.mkdir(exist_ok=True)
    helper_sock = workdir / "helper.sock"
    run_dir = workdir / "root-run"
    label = args.label
    recorded_pids: set[int] = set()
    phases: list[str] = []

    from services.permissions_helper import client as helper_client

    def root_status() -> dict[str, Any]:
        return _root_call(run_dir, "status")

    def helper_root_status() -> dict[str, Any]:
        return cast("dict[str, Any]", helper_client.root_status(sock_path=helper_sock))

    def phase_pass(name: str, detail: str) -> None:
        phases.append(name)
        print(f"PHASE {name}: PASS ({detail})")

    try:
        # ---- build ---------------------------------------------------------
        print(f"workdir: {workdir}")
        app = workdir / "app" / "AvaPermissionsHelper.app"
        exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
        shutil.rmtree(app, ignore_errors=True)
        exe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            REPO_ROOT / "services" / "permissions_helper" / "helper" / "Info.plist",
            app / "Contents" / "Info.plist",
        )
        _run(
            [
                "swiftc",
                "-O",
                str(REPO_ROOT / "services" / "permissions_helper" / "helper" / "main.swift"),
                "-o",
                str(exe),
            ],
            timeout=300.0,
        )
        exe.chmod(0o755)
        _run(
            ["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(app)],
            timeout=120.0,
        )
        _save(
            evidence,
            "build.txt",
            _run(["swiftc", "--version"]).stdout
            + _run(["codesign", "-dvv", str(app)], check=False).stderr,
        )
        phase_pass("build", f"ad-hoc signed helper at {app}")

        # ---- fixtures ------------------------------------------------------
        # Fresh run surface: stale fixtures/logs from an earlier run must not
        # satisfy this run's waits (the attribution split and the custody
        # refusal check read files, not memory), and a stale run dir's custody
        # records or stop intents would refuse this run's root outright.
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        for stale in [
            *workdir.glob("unit-*"),
            workdir / "root.stdout.log",
            workdir / "root.stderr.log",
            workdir / "helper.stdout.log",
            workdir / "helper.stderr.log",
        ]:
            stale.unlink(missing_ok=True)
        probe = workdir / "unit_probe.py"
        probe.write_text(_UNIT_PROBE)
        manifests = workdir / "manifests.json"
        manifests.write_text(
            json.dumps(
                {
                    "units": [
                        {
                            "id": unit_id,
                            "exec": [
                                args.python,
                                str(probe),
                                str(workdir / f"unit-results-{unit_id}.json"),
                                str(workdir / f"unit-{unit_id}.beat"),
                            ],
                            "restart": "always",
                            "attach": "root",
                        }
                        for unit_id in _UNIT_IDS
                    ]
                }
            )
        )
        seed = {
            "argv": [
                args.python,
                "-m",
                "services.ava_root",
                "--run-dir",
                str(run_dir),
                "--manifests",
                str(manifests),
            ],
            "cwd": str(REPO_ROOT),
            "run_dir": str(run_dir),
            "stdout": str(workdir / "root.stdout.log"),
            "stderr": str(workdir / "root.stderr.log"),
            "env": {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        }
        seed_path = run_dir / "seed.json"
        seed_path.write_text(json.dumps(seed, indent=2))
        plist = {
            "Label": label,
            "ProgramArguments": [str(exe)],
            "EnvironmentVariables": {
                "AVA_PERMISSIONS_HELPER_SOCKET": str(helper_sock),
                "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(seed_path),
                "AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION": "1",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(workdir / "helper.stdout.log"),
            "StandardErrorPath": str(workdir / "helper.stderr.log"),
        }
        plist_path = workdir / f"{label}.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump(plist, handle)

        # ---- launch --------------------------------------------------------
        _bootout(label)
        helper_sock.unlink(missing_ok=True)
        _run(["launchctl", "bootstrap", _launchctl_domain(), str(plist_path)], timeout=60.0)
        _wait_for("helper socket", helper_sock.exists, _HELPER_WAIT_S, "launch")
        ping = _wait_for(
            "helper ping",
            lambda: _ping_or_none(helper_client, helper_sock),
            _HELPER_WAIT_S,
            "launch",
        )
        helper_pid = _wait_for("helper job pid", lambda: _job_pid(label), _HELPER_WAIT_S, "launch")
        recorded_pids.add(helper_pid)
        _save(
            evidence,
            "launchctl-print.txt",
            _run(["launchctl", "print", f"{_launchctl_domain()}/{label}"], check=False).stdout,
        )
        phase_pass("launch", f"helper pid {helper_pid}, ping={ping}")

        # ---- chain ---------------------------------------------------------
        _wait_for("root socket", (run_dir / _ROOT_SOCKET).exists, _ROOT_WAIT_S, "chain")
        status = _wait_for(
            "root running", lambda: _status_if_running(root_status), _ROOT_WAIT_S, "chain"
        )
        root_pid = int(status["root"]["pid"])
        recorded_pids.add(root_pid)
        initial_units = {unit_id: int(_unit_entry(status, unit_id)["pid"]) for unit_id in _UNIT_IDS}
        recorded_pids.update(initial_units.values())

        helper_ppid = _ppid_of(helper_pid)
        root_ppid = _ppid_of(root_pid)
        if root_ppid != helper_pid:
            _fail("chain", f"root ppid {root_ppid} != helper pid {helper_pid}")
        if helper_ppid != 1:
            _fail("chain", f"helper ppid {helper_ppid} is not launchd(1)")
        for unit_id, unit_pid in initial_units.items():
            unit_ppid = _ppid_of(unit_pid)
            if unit_ppid != root_pid:
                _fail("chain", f"unit {unit_id} ppid {unit_ppid} != root pid {root_pid}")
        _save(
            evidence,
            "ps-chain.txt",
            _ps_line(helper_pid, root_pid, *initial_units.values())
            + f"\nhelper ppid={helper_ppid} root ppid={root_ppid}"
            + "".join(f" {unit_id} ppid={_ppid_of(pid)}" for unit_id, pid in initial_units.items()),
        )
        _save(evidence, "root-status-initial.json", json.dumps(status, indent=2))
        _save(evidence, "keeper-status-initial.json", json.dumps(helper_root_status(), indent=2))
        phase_pass(
            "chain",
            f"launchd -> helper {helper_pid} -> root {root_pid} -> units {list(initial_units.values())}",
        )

        # ---- attribution (F11) --------------------------------------------
        if args.skip_attribution:
            print("PHASE attribute: SKIP (--skip-attribution)")
        else:
            unit_results = {}
            for unit_id in initial_units:
                results_path = workdir / f"unit-results-{unit_id}.json"
                _wait_for(
                    f"unit {unit_id} preflight results", results_path.exists, 30.0, "attribute"
                )
                unit_results[unit_id] = json.loads(results_path.read_text())
            time.sleep(2.0)
            for unit_id, results in unit_results.items():
                if int(results["ppid"]) != root_pid:
                    _fail(
                        "attribute", f"unit {unit_id} ppid {results['ppid']} != root pid {root_pid}"
                    )
                _save(evidence, f"unit-results-{unit_id}.json", json.dumps(results, indent=2))
                _save(
                    evidence, f"unit-log-{unit_id}.txt", _tail(run_dir / "logs" / f"{unit_id}.log")
                )
            log_text = _run(
                [
                    "/usr/bin/log",
                    "show",
                    "--last",
                    "5m",
                    "--style",
                    "compact",
                    "--predicate",
                    'eventMessage CONTAINS "AUTHREQ_ATTRIBUTION"',
                ],
                timeout=120.0,
            ).stdout
            _save(evidence, "tccd-attribution-window.txt", log_text)
            attributed = []
            for unit_id, unit_pid in initial_units.items():
                matched = None
                for line in log_text.splitlines():
                    found = _ATTRIBUTION_RE.search(line)
                    if found and int(found.group("requesting_pid")) == unit_pid:
                        matched = found
                        break
                if matched is None:
                    _fail(
                        "attribute",
                        f"no AUTHREQ_ATTRIBUTION line for unit {unit_id} pid {unit_pid}; "
                        "see evidence/tccd-attribution-window.txt",
                    )
                if int(matched.group("responsible_pid")) != helper_pid:
                    _fail(
                        "attribute",
                        f"unit {unit_id} attributed to pid {matched.group('responsible_pid')} "
                        f"({matched.group('responsible_id')}), not helper {helper_pid}",
                    )
                attributed.append(f"{unit_id}={matched.group('responsible_id')}")
            prompting = [
                line
                for line in log_text.splitlines()
                if "AUTHREQ_PROMPTING" in line and re.search(rf"pid={helper_pid}\b", line)
            ]
            if prompting:
                _fail("attribute", f"unexpected permission prompt line: {prompting[0][:200]}")
            phase_pass(
                "attribute", f"units resolve to helper {helper_pid} ({', '.join(attributed)})"
            )

        # ---- conflict (helper crash) --------------------------------------
        # F12b (task #3380): sample the whole tree + TCC attribution across the
        # helper death/replacement window at five points -- h0 before the kill
        # (control: every request must resolve to the old helper), h1 right
        # after it, h2 once the relaunched helper reports `conflict`, h3 after
        # the orphan tree is closed, h4 once the re-seed is stable (re-seeded
        # units must resolve to the new helper). h1-h3 are the measurement:
        # what responsibility looks like between death and replacement.
        conflict_f12: dict[str, Any] = {"points": {}}
        ab_units = list(initial_units.items())
        ab_chain = [helper_pid, root_pid, *initial_units.values()]
        if args.sample_conflict:
            conflict_f12["points"]["h0"] = _f12_point(workdir, "h0", ab_units, ab_chain)
        _kill(helper_pid, signal.SIGKILL)
        if args.sample_conflict:
            conflict_f12["kill_ts"] = round(time.time(), 3)
            conflict_f12["points"]["h1"] = _f12_point(workdir, "h1", ab_units, ab_chain)
        new_helper_pid = _wait_for(
            "helper relaunched",
            lambda: _relaunched_helper(label, helper_pid),
            _HELPER_WAIT_S,
            "conflict",
        )
        recorded_pids.add(new_helper_pid)
        conflict = _wait_for(
            "keeper conflict",
            lambda: _status_if_conflict(helper_root_status),
            _ROOT_WAIT_S,
            "conflict",
        )
        if int(conflict["conflict"]["pid"]) != root_pid:
            _fail("conflict", f"conflict pid {conflict['conflict']['pid']} != orphan {root_pid}")
        _save(evidence, "keeper-status-conflict.json", json.dumps(conflict, indent=2))
        if args.sample_conflict:
            conflict_f12["points"]["h2"] = _f12_point(
                workdir, "h2", ab_units, [new_helper_pid, *ab_chain]
            )

        # The helper never signals a root it did not seed: a plain stop and a
        # forced one (raw wire -- the client has no force) are both refused.
        refusals = (
            _refusal(lambda: helper_client.stop_root(sock_path=helper_sock)),
            _refusal(lambda: helper_client._call("root_stop", force=True, sock_path=helper_sock)),
        )
        if "native recovery is required" not in refusals[0] or "force" not in refusals[1]:
            _fail("conflict", f"root_stop over the orphan root was not refused: {refusals}")
        _save(evidence, "conflict-refusals.txt", "\n".join(refusals))

        # Native recovery: the orphan closes its own tree through its control
        # socket; once the run dir is free the keeper seeds a fresh root itself.
        if _root_call(run_dir, "shutdown") != {"shutdown_requested": True}:
            _fail("conflict", "the orphan root did not accept its own shutdown")
        orphan_tree = [root_pid, *initial_units.values()]
        _wait_for(
            "orphan tree closed",
            lambda: not any(_pid_alive(pid) for pid in orphan_tree),
            _RESTART_WAIT_S,
            "conflict",
        )
        if args.sample_conflict:
            conflict_f12["points"]["h3"] = _f12_point(
                workdir, "h3", ab_units, [new_helper_pid, *ab_chain]
            )
        reseeded = _wait_for(
            "reseeded root",
            lambda: _status_if_keeper_running(helper_root_status),
            _ROOT_WAIT_S,
            "conflict",
        )
        reseeded_pid = int(reseeded["pid"])
        recorded_pids.add(reseeded_pid)
        reseeded_status = _wait_for(
            "reseeded tree",
            lambda: _status_if_running(root_status, excluding=root_pid),
            _ROOT_WAIT_S,
            "conflict",
        )
        reseeded_units = {
            unit_id: int(_unit_entry(reseeded_status, unit_id)["pid"]) for unit_id in _UNIT_IDS
        }
        recorded_pids.update(reseeded_units.values())
        if _ppid_of(reseeded_pid) != new_helper_pid:
            _fail("conflict", "reseeded root is not the relaunched helper's child")
        _save(evidence, "keeper-status-reseeded.json", json.dumps(reseeded, indent=2))
        _save(
            evidence,
            "conflict-reseed.txt",
            _ps_line(new_helper_pid, reseeded_pid, *reseeded_units.values())
            + f"\nreseeded root {reseeded_pid} is child of helper {new_helper_pid}",
        )
        if args.sample_conflict:
            time.sleep(_F12_STABLE_S)
            conflict_f12["points"]["h4"] = _f12_point(
                workdir,
                "h4",
                [*ab_units, *reseeded_units.items()],
                [new_helper_pid, reseeded_pid, *reseeded_units.values()],
            )
            conflict_f12.update(
                {
                    "old_helper_pid": helper_pid,
                    "new_helper_pid": new_helper_pid,
                    "orphan_root_pid": root_pid,
                    "reseeded": reseeded_units,
                }
            )
            _f12_finish(
                evidence,
                conflict_f12,
                "f12-conflict",
                [
                    ("h0", list(initial_units.values()), helper_pid),
                    ("h4", list(reseeded_units.values()), new_helper_pid),
                ],
            )
        phase_pass(
            "conflict", f"orphan {root_pid} refused + closed natively; reseeded root {reseeded_pid}"
        )

        # ---- restart (root crash) -----------------------------------------
        f12: dict[str, Any] = {"points": {}}
        survivors = list(reseeded_units.items())
        chain = [new_helper_pid, reseeded_pid, *reseeded_units.values()]
        restarts_before = int(helper_root_status()["restarts"])
        if args.sample_restart:
            f12["points"]["pre"] = _f12_point(workdir, "pre", survivors, chain)
        _kill(reseeded_pid, signal.SIGKILL)
        if args.sample_restart:
            f12["kill_ts"] = round(time.time(), 3)
            f12["points"]["gap"] = _f12_point(workdir, "gap", survivors, chain)
        keeper = _wait_for(
            "replacement root refused",
            lambda: _status_if_refused(helper_root_status, restarts_before),
            _RESTART_WAIT_S,
            "restart",
        )
        if "custody requires reconciliation" not in (workdir / "root.stderr.log").read_text():
            _fail("restart", "the replacement root did not refuse on retained service custody")
        dead_units = {unit_id: pid for unit_id, pid in survivors if not _pid_alive(pid)}
        if dead_units:
            _fail("restart", f"units died with the root: {dead_units}")
        probes = _probe_pids(probe)
        if probes != set(reseeded_units.values()):
            _fail("restart", f"unit probes {sorted(probes)} are not just the survivors")
        if args.sample_restart:
            f12["points"]["refused"] = _f12_point(workdir, "refused", survivors, chain)
            time.sleep(_F12_STABLE_S)
            f12["points"]["stable"] = _f12_point(workdir, "stable", survivors, chain)
            f12.update({"helper_pid": new_helper_pid, "root_pid": reseeded_pid, "keeper": keeper})
            _f12_finish(
                evidence,
                f12,
                "f12-restart",
                [
                    (point, list(reseeded_units.values()), new_helper_pid)
                    for point in ("pre", "gap", "refused", "stable")
                ],
            )
        _save(
            evidence,
            "restart.txt",
            _ps_line(new_helper_pid, *reseeded_units.values())
            + f"\nroot {reseeded_pid} killed; unit probes alive: {sorted(probes)}",
        )
        _save(evidence, "keeper-status-restart.json", json.dumps(keeper, indent=2))
        phase_pass(
            "restart",
            f"root {reseeded_pid} killed; units kept running; replacement refused on custody"
            + ("; F12 sampling saved" if args.sample_restart else ""),
        )

        # ---- reconcile (cold start over a dead generation) -----------------
        # Opt-in (--reconcile-case). The gate lives inside the phase so main()
        # keeps its frozen complexity budget: one call, not a new branch.
        _reconcile_phase(
            enabled=args.reconcile_case,
            workdir=workdir,
            evidence=evidence,
            run_dir=run_dir,
            probe=probe,
            root_status=root_status,
            helper_root_status=helper_root_status,
            phase_pass=phase_pass,
            recorded_pids=recorded_pids,
            new_helper_pid=new_helper_pid,
            reseeded_pid=reseeded_pid,
            reseeded_units=reseeded_units,
        )

        print("\nSMOKE PASS: " + ", ".join(phases))
        return 0
    except SmokeError as failure:
        print(str(failure))
        if helper_sock.exists():
            with contextlib.suppress(Exception):
                helper_client.stop_root(sock_path=helper_sock)
        print(f"helper stderr tail:\n{_tail(workdir / 'helper.stderr.log')}")
        print(f"root stderr tail:\n{_tail(workdir / 'root.stderr.log')}")
        print(f"evidence retained at: {evidence}")
        return 1
    except Exception as unexpected:  # top-level smoke guard
        print(f"FAIL(phase=unexpected): {unexpected!r}")
        print(f"helper stderr tail:\n{_tail(workdir / 'helper.stderr.log')}")
        print(f"root stderr tail:\n{_tail(workdir / 'root.stderr.log')}")
        print(f"evidence retained at: {evidence}")
        return 1
    finally:
        _bootout(label)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            alive = [pid for pid in recorded_pids if _pid_alive(pid)]
            if not alive:
                break
            for pid in alive:
                _kill(pid, signal.SIGTERM)
            time.sleep(0.25)
        for pid in [pid for pid in recorded_pids if _pid_alive(pid)]:
            _kill(pid, signal.SIGKILL)
        if args.cleanup:
            shutil.rmtree(workdir, ignore_errors=True)
