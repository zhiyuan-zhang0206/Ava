#!/usr/bin/env python
"""F5 SMAppService experiment: LWCR staleness on a BTM-registered agent (#3384).

The throwntom-family failure this reproduces: an app registers an SMAppService
agent (plist under Contents/Library/LaunchAgents). Registration creates a
Background Task Management item whose launch constraint (LWCR) pins the app's
code identity as of registration time. An ad-hoc-signed app pins its cdhash, so
once the code on disk changes, every spawn is evaluated against a constraint
the new code cannot satisfy: launchd parks the job in `spawn failed` /
EX_CONFIG(78), retrying silently under KeepAlive.

This scenario builds a stub .app whose main executable (a) can register and
unregister itself as the agent via SMAppService and (b) runs as the agent's
daemon writing a heartbeat file. It installs the app under ~/Applications,
registers it, verifies healthy, then replaces the executable (marker build,
re-signed) and restarts the job. After recording the failure surface it tests
the repair paths:

  restore original binary   does the job heal by itself?
  bootout + bootstrap       the legacy repair, against this job
  register (bare)           re-register as an updated app does at launch
  unregister + register     the definitive re-pin

Phases: build / register / control / inject / restore / re-inject / repair-b /
repair-c / teardown. Teardown always runs: bootout, unregister, remove the
installed app, then LaunchAgents and BTM residue checks.

A previously poisoned item can leak into re-runs at the same app path/label
(see the F5 findings); use --app-name and --agent-label for a fresh floor.

Safety: only the test label `com.ava.test.f5-lwcr-smagent` (or the --agent-label
override) and its throwaway
app are touched; the production helper is never involved. Evidence lands under
--workdir (never /tmp).

Exit codes: 0 = reproduced + repairs characterized; 2 = NO-REPRO (the job
survived the code change); 1 = harness error (evidence still written).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Allow `python scripts/f5_lwcr_smappservice.py` (sys.path[0] = scripts/) to find
# the shared helpers; under pytest pythonpath=["."] this is a redundant no-op.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Shared harness helpers — see scripts/f5_lwcr_common.py.
from scripts.f5_lwcr_common import (
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
    _sign_app,
    _start_lwcr_stream,
    _stop_lwcr_stream,
    _wait_for,
)

STUB_BUNDLE_ID = "com.ava.test.f5-lwcr-stub"
SM_AGENT_LABEL = "com.ava.test.f5-lwcr-smagent"
SM_AGENT_PLIST_NAME = f"{SM_AGENT_LABEL}.plist"
DEFAULT_APP_NAME = "F5LWCRStub-6132.app"

_STUB_SWIFT = r"""// F5 LWCR experiment stub (dev-only, task #3384): registers itself as an
// SMAppService agent and, in daemon mode, writes a heartbeat file so the
// harness can tell whether launchd actually spawned it.
import Foundation
import ServiceManagement

let plistName = "com.ava.test.f5-lwcr-smagent.plist"
let service = SMAppService.agent(plistName: plistName)
let stubVersion = "f5-original"

let args = CommandLine.arguments
let mode = args.count > 1 ? args[1] : "status"
let resultPath = args.count > 2 ? args[2] : nil

func emit(_ message: String) {
    print(message)
    if let path = resultPath {
        try? (message + "\n").write(toFile: path, atomically: true, encoding: .utf8)
    }
}

func die(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    emit("failed: " + message)
    exit(1)
}

switch mode {
case "register":
    do { try service.register() } catch { die("register error: \(error)") }
    emit("register ok status=\(service.status.rawValue) version=\(stubVersion)")
case "unregister":
    do { try service.unregister() } catch { die("unregister error: \(error)") }
    emit("unregister ok status=\(service.status.rawValue) version=\(stubVersion)")
case "status":
    emit("status=\(service.status.rawValue) version=\(stubVersion)")
case "daemon":
    let directory = args.count > 2 ? args[2] : "."
    let heartbeat = URL(fileURLWithPath: directory).appendingPathComponent("probe-heartbeat.log")
    if !FileManager.default.fileExists(atPath: heartbeat.path) {
        _ = FileManager.default.createFile(atPath: heartbeat.path, contents: nil)
    }
    guard let handle = try? FileHandle(forWritingTo: heartbeat) else {
        die("cannot open heartbeat file")
    }
    while true {
        let stamp = ISO8601DateFormatter().string(from: Date())
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: Data("beat \(stamp) pid=\(ProcessInfo.processInfo.processIdentifier)\n".utf8))
        sleep(5)
    }
default:
    die("usage: F5LWCRStub register|unregister|status|daemon <result-path-or-dir>")
}
"""


def _stub_source(workdir: Path, tag: str, *, label: str = SM_AGENT_LABEL) -> Path:
    """Write the stub's Swift source with the version marker and agent label."""
    marker = f"f5-{tag}"
    source = _STUB_SWIFT.replace("f5-original", marker)
    source = source.replace('"com.ava.test.f5-lwcr-smagent.plist"', f'"{label}.plist"')
    if tag != "original" and marker not in source:
        _fail("build", f"version marker not replaced for tag {tag!r}")
    if f'"{label}.plist"' not in source:
        _fail("build", "agent label not substituted into the stub source")
    path = workdir / f"stub-{tag}.swift"
    path.write_text(source)
    return path


def _stub_exe(app: Path) -> Path:
    return app / "Contents" / "MacOS" / "F5LWCRStub"


def _install_stub(
    workdir: Path, binary: Path, app: Path, sign_mode: str, *, label: str = SM_AGENT_LABEL
) -> None:
    """Assemble the throwaway .app at its final path and sign it."""
    if app.exists():
        shutil.rmtree(app)
    exe = _stub_exe(app)
    agents = app / "Contents" / "Library" / "LaunchAgents"
    exe.parent.mkdir(parents=True, exist_ok=True)
    agents.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {
        "CFBundleIdentifier": STUB_BUNDLE_ID,
        "CFBundleExecutable": "F5LWCRStub",
        "CFBundleName": "F5LWCRStub",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1",
        "CFBundleVersion": "1",
        "LSUIElement": True,
    }
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    exe.write_bytes(binary.read_bytes())
    exe.chmod(0o755)
    agent: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [str(exe), "daemon", str(workdir)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(workdir / "probe.stdout.log"),
        "StandardErrorPath": str(workdir / "probe.stderr.log"),
    }
    with (agents / f"{label}.plist").open("wb") as handle:
        plistlib.dump(agent, handle)
    _sign_app(app, sign_mode, identifier=STUB_BUNDLE_ID)


def _replace_exe(app: Path, binary: Path, sign_mode: str, evidence: Evidence, tag: str) -> str:
    """Swap in a new executable, re-sign the bundle; return the new cdhash."""
    exe = _stub_exe(app)
    before = _cdhash(exe)
    exe.write_bytes(binary.read_bytes())
    exe.chmod(0o755)
    _sign_app(app, sign_mode, identifier=STUB_BUNDLE_ID)
    after = _cdhash(exe)
    rc, out = _cap(["codesign", "-dvvv", str(app)], timeout=60.0)
    evidence.note(f"{tag}-codesign.txt", ["codesign", "-dvvv", str(app)], rc, out)
    evidence.save(
        f"{tag}-cdhash.txt", f"before={before}\nafter={after}\nchanged={before != after}\n"
    )
    return after


def _run_stub(app: Path, mode: str) -> tuple[int, str]:
    return _cap([str(_stub_exe(app)), mode], timeout=60.0)


def _sm_service_call(
    app: Path, workdir: Path, mode: str, evidence: Evidence, tag: str
) -> tuple[int, str]:
    """Run a register/unregister; fall back to `open -a` when direct exec refuses."""
    rc, out = _run_stub(app, mode)
    if rc == 0:
        evidence.note(f"{tag}-{mode}.txt", [str(_stub_exe(app)), mode], rc, out)
        return rc, out
    result_path = workdir / f"{tag}-{mode}-result.txt"
    result_path.unlink(missing_ok=True)
    rc_open, out_open = _cap(
        ["open", "-g", "-W", "-a", str(app), "--args", mode, str(result_path)], timeout=60.0
    )
    file_out = result_path.read_text() if result_path.exists() else ""
    combined = (
        f"direct rc={rc}: {out}\n--- open rc={rc_open}: {out_open}\n--- result file:\n{file_out}"
    )
    evidence.note(
        f"{tag}-{mode}.txt",
        [
            "(direct)",
            str(_stub_exe(app)),
            mode,
            "| (fallback)",
            "open",
            "-g",
            "-W",
            "-a",
            str(app),
            "--args",
            mode,
        ],
        max(rc, rc_open),
        combined,
    )
    if file_out.startswith(f"{mode} ok"):
        return 0, combined
    if file_out.startswith("failed:"):
        return 1, combined
    return rc, combined


def _heartbeat_count(workdir: Path) -> int:
    path = workdir / "probe-heartbeat.log"
    if not path.exists():
        return 0
    return sum(
        1 for line in path.read_text(errors="replace").splitlines() if line.startswith("beat ")
    )


def _runs_int(sample: dict[str, Any]) -> int | None:
    runs = sample.get("runs")
    if isinstance(runs, str) and runs.split():
        with contextlib.suppress(ValueError):
            return int(runs.split()[0])
    return None


def _compact(verdict: dict[str, Any]) -> dict[str, Any]:
    return {key: verdict.get(key) for key in ("state", "last_exit_code", "runs", "pid")}


def _snapshot(label: str, workdir: Path, evidence: Evidence, tag: str) -> dict[str, Any]:
    verdict = _job_verdict(label)
    evidence.save(
        f"{tag}-launchctl.txt",
        f"state={verdict['state']} last_exit={verdict['last_exit_code']} "
        f"runs={verdict['runs']} pid={verdict['pid']} heartbeats={_heartbeat_count(workdir)}\n"
        f"---\n{verdict['raw']}",
    )
    return verdict


def _wait_healthy(
    label: str, workdir: Path, timeout: float, phase: str, evidence: Evidence
) -> dict[str, Any]:
    baseline = _heartbeat_count(workdir)

    def healthy() -> dict[str, Any] | None:
        verdict = _job_verdict(label)
        if verdict["state"] == "running" and _heartbeat_count(workdir) > baseline:
            return verdict
        return None

    try:
        return _wait_for("job running with fresh heartbeats", healthy, timeout, phase)
    except F5Error:
        _snapshot(label, workdir, evidence, f"failed-{phase}")
        raise


def _observe(
    label: str, workdir: Path, evidence: Evidence, seconds: float, tag: str
) -> list[dict[str, Any]]:
    """Sample the job verdict every 2s; returns the parsed samples."""
    samples: list[dict[str, Any]] = []
    lines: list[str] = []
    first_raw: str | None = None
    last_raw: str | None = None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        verdict = _job_verdict(label)
        if first_raw is None:
            first_raw = verdict["raw"]
        last_raw = verdict["raw"]
        sample = {
            key: verdict[key] for key in ("state", "job_state", "last_exit_code", "runs", "pid")
        }
        sample["heartbeats"] = _heartbeat_count(workdir)
        samples.append(sample)
        lines.append(
            f"state={sample['state']} job={sample['job_state']} exit={sample['last_exit_code']} "
            f"runs={sample['runs']} pid={sample['pid']} beats={sample['heartbeats']}"
        )
        time.sleep(2.0)
    evidence.save(f"{tag}-observe.txt", "\n".join(lines))
    if first_raw is not None:
        evidence.save(f"{tag}-launchctl-first.txt", first_raw)
    if last_raw is not None:
        evidence.save(f"{tag}-launchctl-last.txt", last_raw)
    return samples


def _classify_inject(samples: list[dict[str, Any]]) -> str:
    """Classify the observe window: healthy / spawn-failed-78 / stuck-spawn / other."""
    if len(samples) < 5:
        return "insufficient-samples"
    if any(sample["state"] == "running" and sample["pid"] for sample in samples[2:]):
        return "healthy"
    tail = samples[-5:]
    if not all(sample["pid"] is None for sample in tail):
        return "other"
    if all(
        sample.get("job_state") == "spawn failed" and sample["last_exit_code"] == 78
        for sample in tail
    ):
        return "spawn-failed-78"
    if all(sample.get("job_state") == "spawn failed" for sample in tail):
        return "spawn-failed-other-code"
    if all(
        sample["state"] in ("xpcproxy", "spawn scheduled") and not sample["last_exit_code"]
        for sample in tail
    ):
        counters = {value for sample in samples if (value := _runs_int(sample)) is not None}
        growth = len(counters) >= 2 and max(counters) > min(counters)
        return "stuck-spawn" if growth else "stuck-spawn-no-retry"
    return "other"


def _probe_health(
    label: str, workdir: Path, evidence: Evidence, timeout: float, tag: str
) -> tuple[bool, dict[str, Any]]:
    """Wait up to timeout for (running + fresh heartbeat); never raises."""
    baseline = _heartbeat_count(workdir)
    deadline = time.monotonic() + timeout
    verdict = _job_verdict(label)
    while time.monotonic() < deadline:
        verdict = _job_verdict(label)
        if verdict["state"] == "running" and _heartbeat_count(workdir) > baseline:
            evidence.save(
                f"{tag}-verdict.txt",
                f"HEALTHY state=running heartbeats>{baseline}\n---\n{verdict['raw']}",
            )
            return True, verdict
        time.sleep(1.0)
    evidence.save(f"{tag}-verdict.txt", f"NOT-HEALTHY\n---\n{verdict['raw']}")
    return False, verdict


def _repair_register_bare(
    app: Path, workdir: Path, label: str, evidence: Evidence
) -> dict[str, Any]:
    """Re-register without unregistering — what an updated app does at launch."""
    rc, _ = _sm_service_call(app, workdir, "register", evidence, "07-register-bare")
    healthy = rc == 0 and _probe_health(label, workdir, evidence, 60.0, "07-register-bare")[0]
    print(f"PHASE repair-register-bare: rc={rc} healthy={healthy}", flush=True)
    return {"rc": rc, "healthy": healthy}


def _repair_unregister_register(
    app: Path, workdir: Path, label: str, evidence: Evidence
) -> dict[str, Any]:
    """The definitive re-pin: drop the item, register afresh."""
    rc_u, _ = _sm_service_call(app, workdir, "unregister", evidence, "08-reregister")
    rc_r, _ = _sm_service_call(app, workdir, "register", evidence, "08-reregister")
    healthy = rc_r == 0 and _probe_health(label, workdir, evidence, 60.0, "08-reregister")[0]
    print(f"PHASE repair-unregister-register: unrc={rc_u} rc={rc_r} healthy={healthy}", flush=True)
    return {"unregister_rc": rc_u, "register_rc": rc_r, "healthy": healthy}


def _preclean(app: Path, label: str, evidence: Evidence) -> None:
    """Remove leftovers from a previous aborted run before starting."""
    rc, out = _bootout(label)
    evidence.note(
        "00-preclean-bootout.txt", ["launchctl", "bootout", f"{_domain()}/{label}"], rc, out
    )
    if app.exists() and _stub_exe(app).exists():
        rc, out = _run_stub(app, "unregister")
        evidence.note("00-preclean-unregister.txt", [str(_stub_exe(app)), "unregister"], rc, out)
    if app.exists():
        shutil.rmtree(app, ignore_errors=True)


def _residue_checks(app: Path, label: str, evidence: Evidence) -> None:
    rc, out = _cap(["launchctl", "print", f"{_domain()}/{label}"], timeout=30.0)
    evidence.note(
        "zz-residue-launchctl.txt", ["launchctl", "print", f"{_domain()}/{label}"], rc, out
    )
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    matches = sorted(path.name for path in launch_agents.glob(f"*{label}*"))
    evidence.save("zz-residue-launchagents.txt", f"matches in ~/Library/LaunchAgents: {matches}\n")
    candidates = [
        Path.home() / "Library" / "Application Support" / "com.apple.backgroundtaskmanagementagent",
        Path("/private/var/db/com.apple.backgroundtaskmanagement"),
    ]
    lines: list[str] = []
    for directory in candidates:
        lines.append(f"# {directory}: exists={directory.exists()}")
        if not directory.exists():
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            lines.append(f"directory unreadable (expected for the root BTM store): {exc}")
            continue
        for path in entries:
            rc, out = _cap(["grep", "-a", "-c", label, str(path)], timeout=30.0)
            lines.append(f"grep -c {label} {path} -> rc={rc} out={out.strip()}")
    evidence.save("zz-residue-btm.txt", "\n".join(lines))
    evidence.save("zz-residue-app.txt", f"installed app exists: {app.exists()}\n")


def _teardown(app: Path, label: str, evidence: Evidence) -> None:
    rc, out = _bootout(label)
    evidence.note(
        "zz-teardown-bootout.txt", ["launchctl", "bootout", f"{_domain()}/{label}"], rc, out
    )
    if _stub_exe(app).exists():
        rc, out = _run_stub(app, "unregister")
        evidence.note("zz-teardown-unregister.txt", [str(_stub_exe(app)), "unregister"], rc, out)
    if app.exists():
        shutil.rmtree(app, ignore_errors=True)
    _residue_checks(app, label, evidence)


def main() -> int:  # noqa: PLR0915 - one bounded lifecycle: phases, waits, evidence together
    parser = argparse.ArgumentParser(description="F5 SMAppService LWCR experiment (dev-only)")
    parser.add_argument("--workdir", required=True, help="absolute work/evidence dir (never /tmp)")
    parser.add_argument("--install-dir", default=str(Path.home() / "Applications"))
    parser.add_argument("--app-name", default=DEFAULT_APP_NAME)
    parser.add_argument(
        "--agent-label",
        default=SM_AGENT_LABEL,
        help="override the SMAppService label (use a fresh one when a previous run poisoned the item)",
    )
    parser.add_argument("--sign", choices=("adhoc", "cert"), default="adhoc")
    parser.add_argument("--observe-seconds", type=float, default=40.0)
    parser.add_argument("--keep", action="store_true", help="skip teardown (debugging only)")
    parser.add_argument(
        "--prefer-reregister",
        action="store_true",
        help="test unregister+register before the bare re-register",
    )
    args = parser.parse_args()

    workdir = Path(args.workdir)
    parts = workdir.resolve().parts
    on_tmp = parts[1:2] == ("tmp",) or parts[1:3] in (("private", "tmp"), ("var", "folders"))
    if not workdir.is_absolute() or on_tmp:
        print("--workdir must be absolute and not under /tmp or $TMPDIR", file=sys.stderr)
        return 2
    workdir.mkdir(parents=True, exist_ok=True)
    app = Path(args.install_dir) / args.app_name
    evidence = Evidence(workdir)
    label = args.agent_label
    summary: dict[str, Any] = {"label": label, "app": str(app), "sign": args.sign}
    stream: tuple[subprocess.Popen[str], Any] | None = None
    exit_code = 1
    try:
        if args.sign == "cert":
            _cert_signing_context()
        rc, out = _cap(["sw_vers"], timeout=30.0)
        evidence.note("00-sw-vers.txt", ["sw_vers"], rc, out)
        stream = _start_lwcr_stream(evidence, "00-lwcr-stream.log")
        _preclean(app, label, evidence)

        # --- build + install + register
        binary_original = workdir / "stub-original"
        _build_binary(_stub_source(workdir, "original", label=label), binary_original)
        _install_stub(workdir, binary_original, app, args.sign, label=label)
        summary["cdhash_registered"] = _cdhash(_stub_exe(app))
        rc, out = _sm_service_call(app, workdir, "register", evidence, "01")
        summary["register_rc"] = rc
        if rc != 0:
            _fail("register", f"SMAppService register failed: {out[-500:]}")
        status_rc, status_out = _run_stub(app, "status")
        evidence.note("01-status.txt", [str(_stub_exe(app)), "status"], status_rc, status_out)
        if "status=2" in status_out:
            _fail(
                "register", "SMAppService reports requiresApproval(2); headless run cannot proceed"
            )
        _wait_for("agent job to appear", lambda: _job_verdict(label)["state"], 30.0, "register")
        summary["baseline"] = _compact(_wait_healthy(label, workdir, 60.0, "register", evidence))
        print("PHASE register: healthy", flush=True)

        # --- control: a plain kickstart must stay healthy
        rc, out = _cap(["launchctl", "kickstart", "-k", f"{_domain()}/{label}"], timeout=30.0)
        evidence.note(
            "02-control-kickstart.txt",
            ["launchctl", "kickstart", "-k", f"{_domain()}/{label}"],
            rc,
            out,
        )
        summary["control"] = _compact(_wait_healthy(label, workdir, 60.0, "control", evidence))
        print("PHASE control: healthy", flush=True)

        # --- inject: replace the code, re-sign, restart
        binary_marker = workdir / "stub-marker"
        _build_binary(_stub_source(workdir, "marker", label=label), binary_marker)
        summary["cdhash_marker"] = _replace_exe(
            app, binary_marker, args.sign, evidence, "03-inject"
        )
        if summary["cdhash_marker"] == summary["cdhash_registered"]:
            _fail("inject", "marker build did not change the executable cdhash")
        rc, out = _cap(["launchctl", "kickstart", "-k", f"{_domain()}/{label}"], timeout=30.0)
        evidence.note(
            "03-inject-trigger.txt",
            ["launchctl", "kickstart", "-k", f"{_domain()}/{label}"],
            rc,
            out,
        )
        if rc != 0:
            rc, out = _cap(["launchctl", "kill", "SIGKILL", f"{_domain()}/{label}"], timeout=30.0)
            evidence.note(
                "03-inject-kill.txt",
                ["launchctl", "kill", "SIGKILL", f"{_domain()}/{label}"],
                rc,
                out,
            )
        samples = _observe(label, workdir, evidence, args.observe_seconds, "03-inject")
        summary["inject_samples"] = samples
        classification = _classify_inject(samples)
        summary["inject_classification"] = classification
        reproduced = classification in (
            "spawn-failed-78",
            "spawn-failed-other-code",
            "stuck-spawn",
            "stuck-spawn-no-retry",
        )
        summary["inject_result"] = "reproduced" if reproduced else "no-repro"
        print(
            f"PHASE inject: {classification} (runs {samples[0]['runs']} -> {samples[-1]['runs']})",
            flush=True,
        )

        if reproduced:
            # --- restore the original code: does the job heal by itself?
            _replace_exe(app, binary_original, args.sign, evidence, "04-restore")
            rc, out = _cap(["launchctl", "kickstart", "-k", f"{_domain()}/{label}"], timeout=30.0)
            evidence.note(
                "04-restore-trigger.txt",
                ["launchctl", "kickstart", "-k", f"{_domain()}/{label}"],
                rc,
                out,
            )
            healthy, _ = _probe_health(label, workdir, evidence, 60.0, "04-restore")
            summary["restore_healed"] = healthy
            print(f"PHASE restore: healed={healthy}", flush=True)
            # The repair matrix runs against the updated (marker) code, like a real update.
            _replace_exe(app, binary_marker, args.sign, evidence, "05-rebreak")
            rc, out = _cap(["launchctl", "kickstart", "-k", f"{_domain()}/{label}"], timeout=30.0)
            evidence.note(
                "05-rebreak-trigger.txt",
                ["launchctl", "kickstart", "-k", f"{_domain()}/{label}"],
                rc,
                out,
            )
            rebreak = _observe(label, workdir, evidence, 20.0, "05-rebreak")
            summary["rebreak_classification"] = _classify_inject(rebreak)
            print(f"PHASE rebreak: {summary['rebreak_classification']}", flush=True)
            # --- repair B: bootout + bootstrap (the legacy repair path)
            _bootout(label)
            plist = app / "Contents" / "Library" / "LaunchAgents" / f"{label}.plist"
            rc, out = _bootstrap(plist)
            evidence.note(
                "06-repair-bootstrap.txt",
                ["launchctl", "bootstrap", _domain(), str(plist)],
                rc,
                out,
            )
            healthy_b = False
            if rc == 0:
                healthy_b, _ = _probe_health(label, workdir, evidence, 60.0, "06-repair-bootstrap")
            summary["repair_bootout_bootstrap"] = {"rc": rc, "healthy": healthy_b}
            print(f"PHASE repair-bootout-bootstrap: rc={rc} healthy={healthy_b}", flush=True)
            _bootout(label)
            # --- repair C: re-registration. --prefer-reregister tests the blunt
            # unregister+register first; the default tests the bare re-register an
            # updated app performs at launch, falling back on the other.
            if args.prefer_reregister:
                summary["repair_reregister"] = _repair_unregister_register(
                    app, workdir, label, evidence
                )
                if not summary["repair_reregister"]["healthy"]:
                    summary["repair_register_bare"] = _repair_register_bare(
                        app, workdir, label, evidence
                    )
            else:
                summary["repair_register_bare"] = _repair_register_bare(
                    app, workdir, label, evidence
                )
                if not summary["repair_register_bare"]["healthy"]:
                    summary["repair_reregister"] = _repair_unregister_register(
                        app, workdir, label, evidence
                    )
        exit_code = 0 if reproduced else 2
    except F5Error as exc:
        print(str(exc), file=sys.stderr)
        summary["error"] = str(exc)
        exit_code = 1
    finally:
        if args.keep:
            print(f"--keep: teardown SKIPPED; leftovers under {app}", file=sys.stderr)
        else:
            try:
                _teardown(app, label, evidence)
                print("PHASE teardown: done", flush=True)
            except Exception as exc:
                summary["teardown_error"] = f"{type(exc).__name__}: {exc}"
                print(f"PHASE teardown: FAILED {summary['teardown_error']}", file=sys.stderr)
        if stream is not None:
            try:
                _stop_lwcr_stream(*stream)
            except Exception as exc:
                summary["stream_stop_error"] = f"{type(exc).__name__}: {exc}"
        (workdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    printable = {
        key: value
        for key, value in summary.items()
        if key not in ("raw", "inject_samples", "baseline", "control")
    }
    print(f"SUMMARY: {json.dumps(printable)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
