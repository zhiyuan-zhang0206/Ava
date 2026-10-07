"""Build the golden macOS image the Tart verification runs clone from.

    python3 scripts/verify/tart_golden.py --ref origin/main --dry-run   # print every step
    python3 scripts/verify/tart_golden.py --ref origin/main             # build, user present

Done once, with a human at the machine. Everything is automated except the two desktop
grants, which macOS lets only a person give:

1. clone the `macos-tahoe-base` image by digest (SIP disabled; the pull is the slow part),
   4 CPUs and 8 GB;
2. boot it headless with the commit's export as the one read-only share, fetch the commit
   into `~/.ava/source` (the helper is built from it), run the repository's own macOS
   provisioning (Homebrew postgresql@17, redis@8.2, pgbouncer, pgvector; the pinned uv)
   and `uv sync`, which also warms the uv cache every clone inherits;
3. create the helper's self-signed identity, then set the key's partition list, the one
   step the start path does not do: on a headless guest signing otherwise blocks on a
   dialog nobody can answer (the account password below is the base image's documented
   default, in a VM that holds nothing);
4. build, sign and load the permissions helper (`lifecycle.converge()`); it answers `ping`
   with both grant booleans false;
5. shut the guest down and boot it again with graphics: a window opens, this script prints
   what to click, and waits;
6. after you confirm, restart the helper (macOS applies a Screen Recording grant only to a
   process that started after it), and verify: `ping` reports both booleans true, the
   system TCC rows are allowed with the identity's code requirement, and a real capture
   through the helper is not a uniform image;
7. remove `~/.ava/source` (the golden image carries no source and no cluster; each run
   fetches its own commit) and shut the guest down.

Nothing of the host's `~/.ava`, launchd, keychain or TCC is touched, and at most two Tart
VMs run at once. A VM that already has the name is never overwritten. If a step fails the
VM is left stopped as it is, for inspection. The design is
future/infra/engineering/verification-boundaries.md.

Host-side and stdlib-only. Run it from the repository checkout; the commit only decides
which helper source is built.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

RECIPE = Path(__file__).resolve().parent
REPO = RECIPE.parents[1]
sys.path.insert(0, str(REPO))

from scripts.verify.boundary import Evidence, resolve_commit  # noqa: E402 — standalone script
from scripts.verify.tart_vm import (  # noqa: E402 — standalone script
    GUEST_SOURCE,
    PREPARE_SOURCE,
    PYTHON,
    TOOLCHAIN,
    Tart,
    export_commit,
    guest_script,
)

# `latest` as of 2026-10-01; the digest, not the tag, is what a golden image is built from.
IMAGE = "ghcr.io/cirruslabs/macos-tahoe-base@sha256:1b093499716409d29e8b5336844528e1cae375db97d2ad8e5aeff78cf0da201e"
NAME = "ava-golden"
CPUS = 4
MEMORY_MB = 8192
# The base image's documented default account; the VM holds no secret.
ACCOUNT_PASSWORD = "admin"  # noqa: S105 — the base image's documented default account
CERT_CN = "Ava Permissions Helper Code Signing"
BUNDLE_ID = "com.ava.permissions-helper"
TCC_DB = "/Library/Application Support/com.apple.TCC/TCC.db"
# The helper's two desktop grants, as the system TCC database names them.
GRANTED = {"kTCCServiceScreenCapture": 2, "kTCCServiceAccessibility": 2}

_HELPER_LIFECYCLE = "from services.desktop.permissions_helper import lifecycle; lifecycle.{call}"
IDENTITY = guest_script(
    f'cd "{GUEST_SOURCE}" && AVA_CONFIG_FETCH=skip .venv/bin/python -c '
    f'"{_HELPER_LIFECYCLE.format(call="ensure_signing_cert()")}"'
)
KEY_ACCESS = guest_script(
    f"security set-key-partition-list -S apple-tool:,apple: -s -l '{CERT_CN}' "
    f'-k {ACCOUNT_PASSWORD} "$HOME/Library/Keychains/login.keychain-db"'
)
HELPER = guest_script(
    f'cd "{GUEST_SOURCE}" && AVA_CONFIG_FETCH=skip .venv/bin/python -c '
    f'"{_HELPER_LIFECYCLE.format(call="converge()")}"'
)
# Restart the helper through launchd: its job label names the home, so read it from the
# LaunchAgent the converge wrote.
RESTART_HELPER = guest_script(
    'label=$(ls "$HOME/Library/LaunchAgents" | sed -n "s/^\\(com.ava.permissions-helper.*\\)\\.plist$/\\1/p")\n'
    'test -n "$label"\n'
    'launchctl kickstart -k "gui/$(id -u)/$label"\n'
    "sleep 5"
)
# One JSON line for the host to judge: the helper's ping, the identity, its designated
# requirement and CDHash, the system TCC rows (with the code requirement blob) and a
# capture through the helper reduced to colour statistics. Runs in the checkout's venv.
_VERIFY_PY = """
import collections, json, struct, subprocess
from base.paths import permissions_helper_app_dir
from services.desktop.permissions_helper import client

def out(*argv):
    return subprocess.run(argv, capture_output=True, text=True).stdout

app = str(permissions_helper_app_dir() / "AvaPermissionsHelper.app")
record = {"ping": dict(client.ping())}
record["identity"] = out("security", "find-identity", "-p", "codesigning")
signing = subprocess.run(["codesign", "-dv", "-r-", "--verbose=4", app], capture_output=True, text=True)
record["signature"] = [
    line for line in (signing.stdout + signing.stderr).splitlines()
    if line.startswith(("designated =>", "CDHash="))
]
rows = out(
    "sqlite3", "-readonly", "@TCC_DB@",
    "select service, auth_value, hex(csreq) from access where client = '@BUNDLE_ID@'",
)
record["tcc"] = {
    row.split("|")[0]: [int(row.split("|")[1]), row.split("|")[2]] for row in rows.splitlines()
}
path = "/tmp/ava-golden-capture.png"
try:
    client.screencapture_region(0, 0, 1024, 768, path)
    subprocess.run(["/usr/bin/sips", "-s", "format", "bmp", path, "--out", path + ".bmp"],
                   capture_output=True, check=True)
    data = open(path + ".bmp", "rb").read()
    offset = struct.unpack_from("<I", data, 10)[0]
    step = struct.unpack_from("<H", data, 28)[0] // 8
    pixels = data[offset:]
    colours = collections.Counter(pixels[i:i + 3] for i in range(0, len(pixels) - step, step))
    total = sum(colours.values())
    record["capture"] = {"distinct": len(colours), "top_share": colours.most_common(1)[0][1] / total}
except Exception as error:
    record["capture"] = {"error": repr(error)}
print("RECORD " + json.dumps(record))
"""
VERIFY = guest_script(
    f"cd \"{GUEST_SOURCE}\" && AVA_CONFIG_FETCH=skip .venv/bin/python - <<'PY'\n"
    + _VERIFY_PY.replace("@TCC_DB@", TCC_DB).replace("@BUNDLE_ID@", BUNDLE_ID)
    + "PY\n"
)
FINALIZE = guest_script('rm -rf "$HOME/.ava/source"')

INSTRUCTIONS = f"""
A Tart window has opened with the golden image. Do this in that window (about a minute):

  1. System Settings > Privacy & Security > Screen & System Audio Recording:
     turn on AvaPermissionsHelper.
  2. Privacy & Security > Accessibility: turn on AvaPermissionsHelper.
  3. Authenticate with the account password ({ACCOUNT_PASSWORD}) when asked.
  4. If macOS offers "Quit & Reopen", choose it. Never choose "Later".

Both entries are already listed (the helper registers itself at start); each needs only
its toggle. Do not touch anything else in the guest. When both are on, come back here and
press Enter: this script restarts the helper and checks the result.
"""

TIMEOUTS = {
    "clone": 10800,
    "configure": 60,
    "prepare-source": 300,
    "toolchain": 1800,
    "python": 1200,
    "identity": 120,
    "key-access": 120,
    "helper": 600,
    "restart-helper": 120,
    "verify": 180,
    "finalize": 60,
}


def _signing_sha1(record: dict[str, Any]) -> str | None:
    """The 40-hex SHA-1 of the signing identity in `security find-identity` output."""
    return next(
        (word for word in record["identity"].split() if len(word) == 40 and word.isalnum()), None
    )


def _identity_problems(record: dict[str, Any], sha1: str | None) -> list[str]:
    if sha1 is None:
        return ["the signing identity is not in the keychain"]
    designated = [line for line in record["signature"] if line.startswith("designated =>")]
    if not any(f'H"{sha1.lower()}"' in line for line in designated):
        return [f"the designated requirement does not pin the identity: {designated}"]
    return []


def _tcc_problems(record: dict[str, Any], sha1: str | None) -> list[str]:
    problems: list[str] = []
    for service, allowed in GRANTED.items():
        row = record["tcc"].get(service)
        if row is None or row[0] != allowed:
            problems.append(f"the system TCC row for {service} is {row}, expected auth {allowed}")
        elif sha1 is not None and sha1.upper() not in row[1]:
            problems.append(f"the TCC row for {service} does not carry the identity's requirement")
    return problems


def _capture_problems(capture: dict[str, Any]) -> list[str]:
    if "error" in capture:
        return [f"no capture through the helper: {capture['error']}"]
    if capture["distinct"] <= 50 or capture["top_share"] >= 0.95:
        return [f"the capture is a uniform image: {capture}"]
    return []


def judge_grant(record: dict[str, Any]) -> list[str]:
    """Why a golden image's helper is not ready to be cloned; empty when it is."""
    ping = record["ping"]
    sha1 = _signing_sha1(record)
    return [
        *(
            f"the helper reports {key} = {ping[key]}"
            for key in ("preflight_screen", "ax_trusted")
            if ping[key] is not True
        ),
        *_identity_problems(record, sha1),
        *_tcc_problems(record, sha1),
        *_capture_problems(record["capture"]),
    ]


class Build:
    """The golden build's steps: run for real, or only printed with `--dry-run`."""

    def __init__(self, tart: Tart, evidence: Evidence | None) -> None:
        self.tart = tart
        self.evidence = evidence

    def step(self, name: str, argv: list[str], *, stdin: str | None = None) -> None:
        if self.evidence is None:
            print(f"[dry-run] step {name}: {' '.join(argv)}", flush=True)
        else:
            self.evidence.step(name, argv, stdin=stdin)

    def guest(self, name: str, vm: str, script: str, *args: str) -> None:
        self.step(name, self.tart.exec_argv(vm, script, *args))

    def verify(self, vm: str) -> list[str]:
        """Restart the helper and judge it; the record is kept in the evidence directory."""
        self.guest("restart-helper", vm, RESTART_HELPER)
        self.guest("verify", vm, VERIFY)
        if self.evidence is None:
            return []
        log = (self.evidence.root / self.evidence.result["steps"][-1]["log"]).read_text()
        record = json.loads(
            next(line for line in log.splitlines() if line.startswith("RECORD "))[len("RECORD ") :]
        )
        (self.evidence.root / "golden-record.json").write_text(json.dumps(record, indent=2) + "\n")
        return judge_grant(record)


def await_grant(build: Build, vm: str) -> None:
    """Show the instructions, wait for the person, and verify until it holds or they quit."""
    print(INSTRUCTIONS, flush=True)
    while True:
        if build.evidence is None:
            print("[dry-run] wait for you to press Enter, then restart and verify the helper")
            build.verify(vm)
            return
        if input("Press Enter when both toggles are on (q to give up): ").strip().lower() == "q":
            raise SystemExit("gave up before the helper was granted; the VM is left as it is")
        problems = build.verify(vm)
        if not problems:
            return
        print("The grant is not in place yet:\n  - " + "\n  - ".join(problems), flush=True)


def build_golden(ref: str, evidence_root: Path, *, name: str, repo: Path, tart: Tart) -> int:
    dry_run = tart.dry_run
    commit = resolve_commit(repo, ref)
    # Refuse before anything is created: an existing VM is never overwritten, and a third
    # running VM is never booted.
    tart.require_absent(name)
    tart.require_capacity()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    evidence: Evidence | None = None
    if not dry_run:
        evidence = Evidence(
            evidence_root / f"golden-{stamp}-{commit[:8]}",
            {
                "ref": ref,
                "commit": commit,
                "image": IMAGE,
                "vm": name,
                "steps": [],
                "result": "running",
            },
            TIMEOUTS,
        )
        evidence.root.mkdir(parents=True, mode=0o700)
        print(f"evidence: {evidence.root}", flush=True)
    print(f"commit: {commit}\nvm: {name}\nimage: {IMAGE}", flush=True)
    build = Build(tart, evidence)
    # A dry run creates nothing, not even the temporary export it would mount.
    export = (
        Path(tempfile.gettempdir()) / "ava-golden-src-dry-run"
        if dry_run
        else Path(tempfile.mkdtemp(prefix="ava-golden-src-"))
    )
    proc = None
    try:
        if not dry_run:
            export_commit(repo, commit, export)
        build.step("clone", tart.argv("clone", IMAGE, name))
        build.step(
            "configure", tart.argv("set", name, "--cpu", str(CPUS), "--memory", str(MEMORY_MB))
        )
        log = (evidence.root if evidence else export) / "tart-run-headless.log"
        proc = tart.boot(name, log, share=export, graphics=False)
        tart.wait_ready(name, proc)
        build.guest("prepare-source", name, PREPARE_SOURCE, commit)
        build.guest("toolchain", name, TOOLCHAIN)
        build.guest("python", name, PYTHON)
        build.guest("identity", name, IDENTITY)
        build.guest("key-access", name, KEY_ACCESS)
        build.guest("helper", name, HELPER)
        tart.shutdown_guest(name, proc)
        log = (evidence.root if evidence else export) / "tart-run-graphics.log"
        proc = tart.boot(name, log, share=None, graphics=True)
        tart.wait_ready(name, proc)
        await_grant(build, name)
        build.guest("finalize", name, FINALIZE)
        tart.shutdown_guest(name, proc)
    except BaseException as error:
        if evidence is not None:
            evidence.result["error"] = repr(error)
            evidence.result["result"] = "failed"
            evidence.save()
        raise
    finally:
        tart.halt(name, proc)
        if not dry_run:
            shutil.rmtree(export, ignore_errors=True)
    if evidence is not None:
        evidence.result["result"] = "passed"
        evidence.save()
        print(f"golden image {name} is ready and stopped ({evidence.root / 'result.json'})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True, help="commit whose helper source is built")
    parser.add_argument("--name", default=NAME, help="name of the golden image to create")
    parser.add_argument(
        "--tart",
        default=shutil.which("tart") or str(Path.home() / ".local/bin/tart"),
        help="the Tart binary (default: tart on PATH, else ~/.local/bin/tart)",
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path(tempfile.gettempdir()) / "ava-verify",
        help="parent directory of the build's evidence directory",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print every step; run nothing and touch nothing"
    )
    args = parser.parse_args()
    if not args.dry_run:
        args.evidence_root.mkdir(parents=True, exist_ok=True)
    return build_golden(
        args.ref,
        args.evidence_root.resolve(),
        name=args.name,
        repo=REPO,
        tart=Tart(args.tart, dry_run=args.dry_run),
    )


if __name__ == "__main__":
    sys.exit(main())
