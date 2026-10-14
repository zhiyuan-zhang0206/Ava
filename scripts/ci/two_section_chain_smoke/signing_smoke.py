"""The helper signing smoke of the macOS CI lane (job `helper-signing-smoke`).

`python -m scripts.ci.two_section_chain_smoke.signing_smoke run --keychain-password PW` builds the
real permissions-helper app from this checkout, signs it ad-hoc, reads the designated requirement and
the AppleEvents entitlement back through the real `codesign`, then bootstraps it under launchd and
pings it. `... signing_smoke label` prints the launchd label the lane tears down.

Hosted macOS runners cannot codesign with an imported identity (platform limitation,
runner-images#13884 family; all lookup paths return errSecItemNotFound). Real signing verification
is carried by the production preflight hard gate on real hosts. This lane signs ad-hoc so the
build, identity, partition, launchd and ping chain stays real. The keychain is the lane's isolated
throwaway one (`AVA_PERMISSIONS_HELPER_KEYCHAIN`); its password protects nothing and is passed in.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from base.paths import permissions_helper_app_dir
from base.sessions.helperproc import helper_client
from services.desktop.permissions_helper import lifecycle

APPLE_EVENTS_ENTITLEMENT = "com.apple.security.automation.apple-events"


def readback_failure(what: str, returncode: int, text: str, expected: str) -> str | None:
    """Why a `codesign -d` readback does not show `expected`, or None when it does."""
    if returncode != 0:
        return f"{what} readback failed: {text[:500]}"
    if expected not in text:
        return f"{what} readback has no {expected!r}: {text[:500]}"
    return None


def _security(*args: str, check: bool = True) -> None:
    subprocess.run(["security", *args], check=check)  # noqa: S603 - fixed argv, macOS tool


def _codesign_readback(*args: str) -> tuple[int, str]:
    result = subprocess.run(  # noqa: S603 - fixed argv, macOS tool
        ["codesign", *args], capture_output=True, check=False, text=True
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def import_signing_identity(keychain_password: str) -> None:
    """Import the signing identity into the isolated keychain and let codesign use its key."""
    lifecycle.ensure_signing_cert()
    keychain = str(lifecycle.keychain_path())
    _security(
        "set-key-partition-list",
        "-S",
        "apple-tool:,apple:,codesign:",
        "-s",
        "-k",
        keychain_password,
        keychain,
    )
    # Report what the lane can see: valid identities, all identities (including invalid ones),
    # whether the imported key and certificate are registered, and the search list that later
    # signing commands use.
    _security("find-identity", "-v", "-p", "codesigning")
    _security("find-identity", "-p", "codesigning")
    _security("dump-keychain", keychain)
    _security("list-keychains")
    # Report the lock state without failing the lane, then unlock again in case the import relocked it.
    _security("show-keychain-info", keychain, check=False)
    _security("unlock-keychain", "-p", keychain_password, keychain)


def build_app() -> Path:
    """Compile and assemble the real helper app bundle."""
    build_dir = permissions_helper_app_dir()
    app = build_dir / "AvaPermissionsHelper.app"
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / "AvaPermissionsHelper"
    lifecycle.run_tool(["swiftc", "-O", str(lifecycle.SOURCE), "-o", str(binary)])
    lifecycle.remove_app(app)
    exe = lifecycle.app_executable(app)
    exe.parent.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(lifecycle.INFO_PLIST.read_bytes())
    exe.write_bytes(binary.read_bytes())
    exe.chmod(0o755)
    return app


def sign_adhoc(app: Path) -> None:
    """Sign without asking the hosted runner to resolve the imported identity."""
    lifecycle.run_tool(
        [
            "codesign",
            "--force",
            "--sign",
            "-",
            "--identifier",
            lifecycle.HELPER_BUNDLE_ID,
            # Production signs the artifact with the AppleEvents entitlement; keep this lane
            # on the same signing policy so codesign validates the entitlements plist on
            # every PR.
            "--entitlements",
            str(lifecycle.ENTITLEMENTS),
            str(app),
        ]
    )


def verify_signature(app: Path) -> None:
    """Read the designated requirement and the entitlement back through the real `codesign`.

    The ad-hoc signature's designated requirement has no "designated =>" line, so the check is on
    the raw readback (`lifecycle.read_dr` parses the stable-identity shape and is covered by unit
    tests and the production preflight gate). The entitlement must land in the signature, not only
    on the command line.
    """
    returncode, dr_text = _codesign_readback("-d", "-r-", str(app))
    print(f"DR readback: {dr_text[:500]}")
    problem = readback_failure("designated requirement", returncode, dr_text, "cdhash")
    if problem is None:
        returncode, ent_text = _codesign_readback("-d", "--entitlements", ":-", str(app))
        problem = readback_failure("entitlements", returncode, ent_text, APPLE_EVENTS_ENTITLEMENT)
    if problem is not None:
        raise SystemExit(problem)


def load_and_ping(app: Path) -> None:
    """Bootstrap the helper under launchd and ping it.

    A bootstrap failure intentionally fails the lane: the hosted runner's user launchd chain is
    part of the production contract being tested.
    """
    lifecycle.install_and_load(app, rebuilt=True, helper=helper_client)
    if helper_client().ping()["pong"] is not True:
        raise SystemExit("the helper did not answer the ping")


def run(keychain_password: str) -> None:
    import_signing_identity(keychain_password)
    app = build_app()
    sign_adhoc(app)
    verify_signature(app)
    load_and_ping(app)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="build, sign, read back, launch and ping")
    run_parser.add_argument(
        "--keychain-password", required=True, help="password of the isolated signing keychain"
    )
    commands.add_parser("label", help="print the launchd label of the helper job")
    args = parser.parse_args(argv)
    if args.command == "label":
        print(lifecycle.label())
    else:
        run(args.keychain_password)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
