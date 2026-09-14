#!/usr/bin/env python3
"""F5 fault injection: launchd LWCR/EX_CONFIG(78) staleness + bootout+bootstrap repair.

Dev-only harness (task #3384, parent #3195). It builds a throwaway
ad-hoc-signed permissions-helper .app (same machinery as
scripts/two_section_chain_smoke.py), bootstraps it under a test launchd label,
then injects the macOS 26 LWCR staleness condition: the signed executable is
replaced under the already-loaded job and the job is restarted. It records the
spawn-failed / EX_CONFIG(78) symptoms (including the silence under KeepAlive
throttle), then verifies the documented repair (`launchctl bootout` +
`launchctl bootstrap`) and the half-failure paths (bootstrap-while-loaded,
bootout-when-absent, bootstrap-with-missing-plist).

Safety: throwaway build + test label only; the production helper and its launchd
job are never touched; the test job is always booted out (never disabled) and,
in --plist launchagents mode, its test plist is removed again. Evidence is
written under --workdir and never under /tmp.

Signing: --sign adhoc uses an ad-hoc signature (no keychain); --sign cert mirrors
production's stable certificate + pinned designated requirement, gated on
lifecycle.preflight_signing_smoke() (refuses instead of prompting).
Plist: --plist launchagents (default) puts the test plist in
~/Library/LaunchAgents so the BTM/LWCR path engages like production;
--plist workdir keeps it isolated inside the workdir.
Replace: --replace exe (default) rewrites the executable in place; bundle
replaces the whole .app like lifecycle.build_and_sign does.

Modes:
  --inject resign   (default) rebuild with a marker, re-sign, expect the repro.
  --inject control  rebuild unchanged, re-sign; a healthy job must stay healthy
                    (proves the trigger is the identity change, not the act of
                    kickstarts/re-signing).

Phases: build / launch / inject / repair / teardown (teardown always runs).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Allow `python scripts/lwcr_fault_injection.py` (sys.path[0] = scripts/) to find the
# shared helpers; under pytest pythonpath=["."] this is a redundant no-op.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Shared harness helpers — both F5 scenarios (this one and
# scripts/f5_lwcr_smappservice.py) import them from scripts/f5_lwcr_common.py.
from scripts.f5_lwcr_common import (
    _LAUNCH_AGENTS_DIR,
    REPO_ROOT,
    Evidence,
    F5Error,
    _bootout,
    _bootstrap,
    _build_binary,
    _cap,
    _cdhash,
    _cert_signing_context,
    _domain,
    _fail,
    _job_verdict,
    _log_window,
    _sign_app,
    _start_lwcr_stream,
    _stop_lwcr_stream,
    _wait_for,
)

DEFAULT_LABEL = "com.ava.test.f5-lwcr"
_SOURCE = REPO_ROOT / "services" / "permissions_helper" / "helper" / "main.swift"
_INFO_PLIST = REPO_ROOT / "services" / "permissions_helper" / "helper" / "Info.plist"
_HELPER_WAIT_S = 30.0
_THROTTLE_OBSERVE_S = 30.0


def _client() -> Any:
    from services.permissions_helper import client

    return client


def _ping(sock: Path) -> dict[str, Any] | None:
    try:
        reply = _client().ping(sock_path=str(sock))
    except Exception:  # unreachable / protocol error all mean "not serving"
        return None
    return reply if isinstance(reply, dict) and reply.get("pong") is True else None


def _assemble_app(workdir: Path, binary: Path, sign_mode: str) -> Path:
    app = workdir / "app" / "AvaPermissionsHelper.app"
    exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_INFO_PLIST, app / "Contents" / "Info.plist")
    exe.write_bytes(binary.read_bytes())
    exe.chmod(0o755)
    _sign_app(app, sign_mode)
    return app


def _injection_source(workdir: Path, mode: str) -> Path:
    text = _SOURCE.read_text()
    if mode == "resign":
        needle = '"root seed: env must be a map of string to string"'
        if needle not in text:
            _fail("inject", "marker needle missing from main.swift")
        text = text.replace(
            needle, '"root seed: env must be a map of string to string (f5 injection)"'
        )
    dst = workdir / "injection-source.swift"
    dst.write_text(text)
    return dst


def _plist_path_for(workdir: Path, label: str, location: str) -> Path:
    if location == "launchagents":
        return _LAUNCH_AGENTS_DIR / f"{label}.plist"
    return workdir / f"{label}.plist"


def _write_plist(workdir: Path, label: str, exe: Path, sock: Path, location: str) -> Path:
    plist = {
        "Label": label,
        "ProgramArguments": [str(exe)],
        "EnvironmentVariables": {
            "AVA_PERMISSIONS_HELPER_SOCKET": str(sock),
            "AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(workdir / "helper.stdout.log"),
        "StandardErrorPath": str(workdir / "helper.stderr.log"),
    }
    path = _plist_path_for(workdir, label, location)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(plist, handle)
    return path


def main() -> int:  # noqa: PLR0915 - one bounded lifecycle: phases, waits, evidence together
    parser = argparse.ArgumentParser(description="F5 LWCR/EX_CONFIG fault injection (dev-only)")
    parser.add_argument("--workdir", required=True, help="absolute work/evidence dir (never /tmp)")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--inject", choices=("resign", "control"), default="resign")
    parser.add_argument("--trigger", choices=("kickstart", "sigkill"), default="kickstart")
    parser.add_argument("--sign", choices=("adhoc", "cert"), default="adhoc")
    parser.add_argument(
        "--inject-sign",
        choices=("same", "adhoc", "none"),
        default="same",
        help="signature of the injected build: same identity, ad-hoc, or none (broken seal)",
    )
    parser.add_argument("--plist", choices=("launchagents", "workdir"), default="launchagents")
    parser.add_argument("--replace", choices=("exe", "bundle"), default="exe")
    parser.add_argument("--cleanup", action="store_true", help="delete the workdir at the end")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    parts = workdir.resolve().parts
    on_tmp = parts[1:2] == ("tmp",) or parts[1:3] in (("private", "tmp"), ("var", "folders"))
    if not workdir.is_absolute() or on_tmp:
        print("--workdir must be absolute and not under /tmp or $TMPDIR", file=sys.stderr)
        return 2
    workdir.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(workdir)
    sock = workdir / "helper.sock"
    label = args.label
    recorded_pids: list[int] = []
    summary: dict[str, Any] = {
        "label": label,
        "inject_mode": args.inject,
        "trigger": args.trigger,
        "sign": args.sign,
        "inject_sign": args.inject_sign,
        "plist_location": args.plist,
        "replace": args.replace,
        "os": _cap(["sw_vers", "-productVersion"], timeout=30.0)[1].strip(),
    }
    exit_code = 1
    app: Path | None = None
    plist_path: Path | None = None

    try:
        # ---- build ---------------------------------------------------------
        if args.sign == "cert":
            try:
                _cert_signing_context()
            except Exception as exc:
                _fail("build", f"cert signing preflight refused: {exc}")
        binary = workdir / "binary-initial"
        _build_binary(_SOURCE, binary)
        app = _assemble_app(workdir, binary, args.sign)
        exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
        cdhash_initial = _cdhash(app)
        summary["cdhash_initial"] = cdhash_initial
        evidence.save("01-build-cdhash-initial.txt", f"cdhash = {cdhash_initial}")
        print(
            f"PHASE build: PASS ({args.sign}-signed helper at {app}, cdhash {cdhash_initial[:12]}...)"
        )

        # ---- launch --------------------------------------------------------
        plist_path = _write_plist(workdir, label, exe, sock, args.plist)
        _bootout(label)
        sock.unlink(missing_ok=True)
        rc, out = _bootstrap(plist_path)
        evidence.note(
            "02-launch-bootstrap.txt",
            ["launchctl", "bootstrap", _domain(), str(plist_path)],
            rc,
            out,
        )
        if rc != 0:
            _fail("launch", f"bootstrap failed (rc={rc}): {out[-400:]}")
        _wait_for("helper socket", sock.exists, _HELPER_WAIT_S, "launch")
        _wait_for("helper ping", lambda: _ping(sock), _HELPER_WAIT_S, "launch")
        pid = _wait_for("helper pid", lambda: _job_verdict(label)["pid"], _HELPER_WAIT_S, "launch")
        recorded_pids.append(pid)
        verdict = _job_verdict(label)
        evidence.save("03-launch-state.txt", verdict["raw"])
        print(f"PHASE launch: PASS (helper pid {pid}, ping=True, state={verdict['state']})")

        # ---- inject --------------------------------------------------------
        summary["state_before"] = verdict["state"]
        injection_source = _injection_source(workdir, args.inject)
        binary_injected = workdir / "binary-injected"
        _build_binary(injection_source, binary_injected)
        inject_sign = args.sign if args.inject_sign == "same" else args.inject_sign
        if args.replace == "bundle":
            shutil.rmtree(app, ignore_errors=True)
            app = _assemble_app(workdir, binary_injected, inject_sign)
            exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
        else:
            exe.write_bytes(binary_injected.read_bytes())
            exe.chmod(0o755)
            _sign_app(app, inject_sign)
        cdhash_injected = _cdhash(app)
        summary["cdhash_injected"] = cdhash_injected
        evidence.save(
            "04-inject-rebuild.txt",
            f"cdhash_before = {cdhash_initial}\ncdhash_injected = {cdhash_injected}",
        )
        if args.inject == "resign" and cdhash_injected == cdhash_initial:
            _fail("inject", "cdhash did not change after the marker rebuild")

        stream_proc, stream_handle = _start_lwcr_stream(evidence, "07d-lwcr-stream.log")
        try:
            time.sleep(1.0)
            if args.trigger == "kickstart":
                rc, out = _cap(
                    ["launchctl", "kickstart", "-k", f"{_domain()}/{label}"], timeout=60.0
                )
                evidence.note(
                    "05-inject-trigger-kickstart.txt",
                    ["launchctl", "kickstart", "-k", f"{_domain()}/{label}"],
                    rc,
                    out,
                )
            else:
                os.kill(pid, signal.SIGKILL)
                evidence.save("05-inject-trigger-sigkill.txt", f"signal.SIGKILL -> pid {pid}")
            time.sleep(8.0)
        finally:
            _stop_lwcr_stream(stream_proc, stream_handle)

        verdict_after = _job_verdict(label)
        evidence.save("06-inject-state.txt", verdict_after["raw"])
        reproduced = (
            verdict_after["state"] == "spawn failed" and verdict_after["last_exit_code"] == 78
        )
        alive = _ping(sock) is not None
        summary.update(
            {
                "state_after_inject": verdict_after["state"],
                "exit_after_inject": verdict_after["last_exit_code"],
                "pid_after_inject": verdict_after["pid"],
                "alive_after_inject": alive,
            }
        )
        _log_window(evidence, "07-lwcr-log.txt", 'eventMessage CONTAINS "LWCR"')
        _log_window(
            evidence, "07b-unable-update-log.txt", 'eventMessage CONTAINS "Unable to get updated"'
        )
        _log_window(
            evidence,
            "07c-job-log-window.txt",
            f'process == "launchd" AND eventMessage CONTAINS "{label}"',
        )

        time.sleep(_THROTTLE_OBSERVE_S)
        verdict_late = _job_verdict(label)
        evidence.save("08-throttle-state.txt", verdict_late["raw"])
        summary["state_after_throttle_window"] = verdict_late["state"]
        summary["exit_after_throttle_window"] = verdict_late["last_exit_code"]
        summary["alive_after_throttle_window"] = _ping(sock) is not None

        if args.inject == "resign" and reproduced:
            print(
                "PHASE inject: PASS (reproduced: state=spawn failed, "
                f"exit={verdict_after['last_exit_code']}, alive={alive})"
            )
            summary["inject_result"] = "reproduced"
        elif args.inject == "resign":
            print(
                "PHASE inject: NO-REPRO (state="
                f"{verdict_after['state']!r}, exit={verdict_after['last_exit_code']}, alive={alive})"
            )
            summary["inject_result"] = "no-repro"
        elif reproduced or verdict_after["state"] != "running":
            print(f"PHASE inject: CONTROL-BROKE (state={verdict_after['state']!r})")
            summary["inject_result"] = "control-broke"
        else:
            print(
                f"PHASE inject: PASS (control held: state={verdict_after['state']}, alive={alive})"
            )
            summary["inject_result"] = "control-held"

        # ---- repair --------------------------------------------------------
        rc, out = _cap(["launchctl", "kickstart", "-k", f"{_domain()}/{label}"], timeout=60.0)
        evidence.note(
            "09-repair-kickstart-only.txt",
            ["launchctl", "kickstart", "-k", f"{_domain()}/{label}"],
            rc,
            out,
        )
        time.sleep(2.0)
        v_kick = _job_verdict(label)
        evidence.save("10-repair-kickstart-only-state.txt", v_kick["raw"])
        summary["kickstart_only_state"] = v_kick["state"]

        rc, out = _bootout(label)
        evidence.note(
            "11-repair-bootout.txt", ["launchctl", "bootout", f"{_domain()}/{label}"], rc, out
        )
        rc, out = _bootstrap(plist_path)
        evidence.note(
            "12-repair-bootstrap.txt",
            ["launchctl", "bootstrap", _domain(), str(plist_path)],
            rc,
            out,
        )
        _wait_for("helper ping after repair", lambda: _ping(sock), _HELPER_WAIT_S, "repair")
        v_fixed = _job_verdict(label)
        evidence.save("13-repair-state.txt", v_fixed["raw"])
        repaired = v_fixed["state"] == "running"
        summary["state_after_repair"] = v_fixed["state"]
        summary["pid_after_repair"] = v_fixed["pid"]
        summary["cdhash_after_repair"] = _cdhash(app)
        if v_fixed["pid"]:
            recorded_pids.append(v_fixed["pid"])
        if repaired:
            print(
                "PHASE repair: PASS (bootout+bootstrap healed: "
                f"state={v_fixed['state']}, ping=True, pid={v_fixed['pid']})"
            )
        else:
            print(f"PHASE repair: UNEXPECTED (state={v_fixed['state']!r} after bootout+bootstrap)")
            summary["repair_result"] = "failed"
            _fail("repair", f"bootout+bootstrap did not heal (state={v_fixed['state']!r})")

        # ---- half-failure paths -------------------------------------------
        rc1, out1 = _bootstrap(plist_path)  # while loaded
        evidence.note(
            "14-half-bootstrap-while-loaded.txt",
            ["launchctl", "bootstrap", _domain(), str(plist_path)],
            rc1,
            out1,
        )
        rc_first, out_first = _bootout(label)
        evidence.note(
            "15a-half-bootout-first.txt",
            ["launchctl", "bootout", f"{_domain()}/{label}"],
            rc_first,
            out_first,
        )
        rc3, out3 = _bootout(label)  # now absent
        evidence.note(
            "15b-half-bootout-when-absent.txt",
            ["launchctl", "bootout", f"{_domain()}/{label}", "(second call)"],
            rc3,
            out3,
        )
        missing = workdir / "missing.plist"
        rc4, out4 = _bootstrap(missing)
        evidence.note(
            "16-half-bootstrap-missing-plist.txt",
            ["launchctl", "bootstrap", _domain(), str(missing)],
            rc4,
            out4,
        )
        summary["half_paths"] = {
            "bootstrap_while_loaded_rc": rc1,
            "bootout_absent_rc": rc3,
            "bootstrap_missing_plist_rc": rc4,
        }
        rc5, out5 = _bootstrap(plist_path)
        evidence.note(
            "17-final-rebootstrap.txt",
            ["launchctl", "bootstrap", _domain(), str(plist_path)],
            rc5,
            out5,
        )
        _wait_for("final ping", lambda: _ping(sock), _HELPER_WAIT_S, "repair")
        print(
            "PHASE repair-halfpaths: PASS (bootstrap-loaded rc="
            f"{rc1}, bootout-absent rc={rc3}, bootstrap-missing rc={rc4})"
        )

        if args.inject == "resign":
            exit_code = 0 if summary.get("inject_result") == "reproduced" and repaired else 3
        else:
            exit_code = 0 if summary.get("inject_result") == "control-held" else 4
    except F5Error as exc:
        print(str(exc), file=sys.stderr)
        summary["error"] = str(exc)
        exit_code = 1
    finally:
        _bootout(label)
        if plist_path is not None and args.plist == "launchagents":
            plist_path.unlink(missing_ok=True)
        for stray in recorded_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(stray, signal.SIGKILL)
        (workdir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        if args.cleanup and exit_code == 0:
            shutil.rmtree(workdir, ignore_errors=True)

    print(f"SUMMARY: {json.dumps({k: v for k, v in summary.items() if k != 'raw'})}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
