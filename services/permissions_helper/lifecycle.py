"""Prepare immutable signed macOS helper artifacts and register one home job.

A stable certificate preserves desktop permission identity. First start may
build and register a helper; repeated start observes an unchanged loaded job.
Replacing its artifact or job requires external stop and exact-home unregister
first. Normal converge never unloads its own possible permission ancestor.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

import shared.paths
from services.permissions_helper import hardened_runtime
from services.permissions_helper.launchd_job import (
    HELPER_BUNDLE_ID,
    clear_helper_stop_intent,
    helper_job_agents_dir,
    helper_job_domain,
    helper_job_label,
    helper_job_plist_path,
    helper_stop_intent,
)
from shared.config import settings
from shared.paths import logs_dir, permissions_helper_socket
from shared.proc import run_bounded

_CERT_CN = "Ava Permissions Helper Code Signing"
_BUNDLE_ID = HELPER_BUNDLE_ID  # alias: the job identity lives in launchd_job
_SERVICE_DIR = Path(__file__).resolve().parent
_SOURCE = _SERVICE_DIR / "helper" / "main.swift"
_INFO_PLIST = _SERVICE_DIR / "helper" / "Info.plist"
_LOCALES = _SERVICE_DIR / "helper" / "locales"
_BUILD_DIR = shared.paths.permissions_helper_app_dir()
_BUILD_STATE_NAME = "build-state.json"
_HELPER_PING_ATTEMPTS = 10
_HELPER_PING_SETTLE_S = 0.5
_IDENTITY_RE = re.compile(
    rf'^\s*\d+\)\s+([0-9A-F]{{40}})\s+"{re.escape(_CERT_CN)}"',
    re.IGNORECASE | re.MULTILINE,
)
_DESIGNATED_REQUIREMENT_RE = re.compile(r"^designated => (.+)$", re.MULTILINE)
_AD_HOC_REFUSAL = (
    "Signing ad-hoc instead would hand the helper a fresh code identity, dropping "
    "the Screen Recording / Accessibility grants TCC keyed on the stable one, so "
    "the build stops here rather than downgrading. Unlock the login keychain on "
    "this host (`security unlock-keychain` from a local Terminal.app or a Screen "
    "Sharing session) and re-run `ava converge`."
)
_ACL_REMEDY = (
    "Give /usr/bin/codesign standing access to the key on this host: in Keychain "
    "Access, open the private key under the login keychain's Keys and set Access "
    "Control to allow all applications, or from a local Terminal.app run "
    f"`security set-key-partition-list -S apple-tool:,apple: -s -l {_CERT_CN!r}` "
    "(it prompts once for the login password). Then re-run `ava converge`."
)
_SIGNING_REACH_REMEDY = (
    "Align the signing keychain with the user search list. From a local Terminal.app, run "
    "`security list-keychains -d user -s <signing keychain path>`. Use the login keychain "
    f"path by default. {_ACL_REMEDY}"
)

# Every tool below is local and has no network leg, so the only way one runs long
# is that it stopped to ask a human. That is not hypothetical: on 2026-08-02 a
# headless `ava update` sat 67 minutes inside `codesign --sign` while macOS held
# up a SecurityAgent dialog asking whether codesign could use the signing key,
# and the whole cluster rollout waited on it. These ceilings turn that wait into
# a failed converge step, which the update path already knows how to abort and
# recover from; the recovery machinery was never the gap, the trigger was. None
# of these is a performance budget -- each sits well above what the work costs.
#
# The bound is applied by `shared.proc.run_bounded`, not `subprocess.run(timeout=)`,
# which kills only the process Python spawned and leaves its descendants running.
# Every git-driving module bounds git through the same helper.
_TIMEOUTS_S = {
    "swiftc": 300.0,  # one small file; the ceiling only catches a wedged toolchain
    "codesign": 60.0,  # signs a few-MB bundle in well under a second
    "security": 30.0,  # keychain queries and a PKCS#12 import, all local
    "openssl": 60.0,  # RSA-2048 keygen + export; slow only on a starved entropy pool
    "launchctl": 30.0,  # local IPC with launchd
}

# Stands in for the exit status of a call the bound killed. 124 is `timeout(1)`'s
# convention and none of the tools above uses it, so `_probe` can hand a hang back
# as a result instead of an exception without colliding with a real exit code.
_TIMED_OUT_RC = 124

# The ACL probe's own ceiling, deliberately far below `_TIMEOUTS_S["codesign"]`:
# its entire job is to decide "would signing hang", so it has to give up while the
# answer is still cheap. Signing a scratch file is sub-second; anything past this
# is the dialog.
_ACL_PROBE_TIMEOUT_S = 20.0


class PermissionsHelperBuildError(RuntimeError):
    """A build, signing, or launchd step failed."""


class PermissionsHelperTimeoutError(PermissionsHelperBuildError):
    """A step outlived its bound and its process tree was killed.

    A subclass rather than a flag because the remedy differs: a step that failed
    fast failed for its own reasons, while a step that ran out the clock was
    waiting on a human, and the caller that knows which tool it invoked can say
    which prompt that was."""


class PermissionsHelperSigningUnavailableError(PermissionsHelperBuildError):
    """The signing key is out of reach of THIS process, before anything is built.

    The stable identity's private key lives in the login keychain, which only a
    process carrying a GUI session's context can open. An interactive shell has
    that context; a converge spawned by the ops pin self-heal does not, so the
    same host signs by hand and refuses under the updater. That makes it a limit
    of the execution context rather than a defect in the build, which is the one
    distinction `_ensure_permissions_helper` needs: it warns and skips on this,
    the way it already does for a host with no swiftc or no display, while a real
    build failure still aborts converge.

    Blocking the whole converge on it takes the cluster down: `ava start` runs
    converge first, so an updater that cannot sign leaves every service stopped
    with nothing to bring them back (2026-08-09 -- a rollout's force-checkout
    freshens main.swift's mtime, which forces the rebuild that reaches for the
    key, so this fires on every rollout rather than rarely)."""


class _BuildState(TypedDict):
    source_hash: str
    dr: str
    signed_at: str


def _run(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run a step that must succeed, raising `PermissionsHelperBuildError` if it
    does not -- including when it outlived its `_TIMEOUTS_S` bound, which is a
    failure of the same kind and is reported as one so the converge step aborts
    rather than blocks."""
    timeout = _TIMEOUTS_S[cmd[0]]
    try:
        proc = run_bounded(cmd, timeout=timeout, capture_output=True)
    except subprocess.TimeoutExpired as exc:
        raise PermissionsHelperTimeoutError(
            f"{cmd[0]} did not finish within {timeout:.0f}s and its process tree was killed; "
            "a local system tool that runs that long is waiting on something that will not "
            "arrive, typically a GUI authorization prompt on a headless host"
        ) from exc
    if proc.returncode != 0:
        raise PermissionsHelperBuildError(
            f"{cmd[0]} failed ({proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc


def _probe(cmd: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run a read-only query, folding a timeout into the returned result.

    Every caller here asks a yes/no question -- is the keychain open, is this
    bundle signed by the stable identity, is that job loaded -- and already reads
    a non-zero exit as "no". A tool that hung is the same "no" with a different
    cause, so it comes back as `_TIMED_OUT_RC` with the reason on stderr instead
    of as an exception raised into paths whose whole point is not to raise. The
    one caller that must tell the two apart compares against `_TIMED_OUT_RC`."""
    bound = _TIMEOUTS_S[cmd[0]] if timeout is None else timeout
    try:
        return run_bounded(cmd, timeout=bound, capture_output=True)
    except subprocess.TimeoutExpired:
        detail = f"`{' '.join(cmd[:2])}` timed out after {bound:.0f}s".encode()
        return subprocess.CompletedProcess(cmd, _TIMED_OUT_RC, b"", detail)


def _keychain_path() -> Path:
    """Return the signing keychain path.

    Defaults to the user's login keychain. CI sets the
    ``AVA_PERMISSIONS_HELPER_KEYCHAIN`` setting to an isolated keychain it
    owns -- the hosted runner's login keychain password is unknowable, so the
    partition-list grant codesign needs cannot be authorized there.
    """
    override = settings.services.permissions_helper_keychain
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Keychains" / "login.keychain-db"


def _signing_keychain_search_list_note() -> str:
    """Describe whether the signing keychain is in the user search list."""
    try:
        keychain = str(_keychain_path())
        proc = _probe(["security", "list-keychains", "-d", "user"])
        if proc.returncode != 0:
            return "The user keychain search list is unreadable."
        listed = {
            line.strip().strip("\"'").strip()
            for line in proc.stdout.decode(errors="replace").splitlines()
        }
    except Exception:  # This diagnostic must never replace the signing refusal.
        return "The user keychain search list is unreadable."
    location = "present in" if keychain in listed else "missing from"
    return f"The signing keychain {keychain!r} is {location} the user keychain search list."


def _keychain_lock_reason() -> str | None:
    """Return why the login keychain cannot serve the signing key, or None.

    The stable identity's private key lives there, and over SSH that keychain is
    normally locked -- codesign then fails deep inside Security.framework with an
    opaque "User interaction is not allowed". Reading its settings first turns
    that into a message that names the fix. `security` cannot unlock it here: the
    only non-interactive form takes the password on argv, where every process on
    the box can read it, so the unlock stays a human step."""
    proc = _probe(["security", "show-keychain-info", str(_keychain_path())])
    if proc.returncode == 0:
        return None
    detail = proc.stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
    return f"the login keychain is not unlocked ({detail})"


def _interactive_signing_reason() -> str | None:
    """Return why signing would block waiting on a human, or None.

    An unlocked keychain is not enough. The signing key carries its own access
    control, and when the binary reaching for it is not covered there,
    Security.framework puts up a SecurityAgent dialog asking a person to allow
    the use. `ensure_signing_cert` imports with `-T /usr/bin/codesign -A`, which
    covers the trusted-application list -- but not the partition list macOS
    checks alongside it, and not a key that predates this import path at all.
    Converge runs headless, so that dialog is never answered and codesign waits
    forever: the 2026-08-02 rollout spent 67 minutes there.

    The only honest way to ask "would this block" is to try it, so this signs a
    throwaway scratch file with the same identity under a short bound -- a
    scratch file rather than the real bundle, so a probe that DOES trip the
    dialog cannot leave the helper half-signed. A timeout is the answer being
    looked for. A non-zero exit is NOT: signing a loose file and signing a bundle
    take different enough paths inside codesign that a failure here is
    inconclusive, and refusing on it would block converge on hosts that sign
    perfectly well. The real sign is bounded too, so an inconclusive probe costs
    a worse error message, never a hang."""
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td) / "acl-probe"
        scratch.write_bytes(b"\x00")
        rc = _probe(
            ["codesign", "--sign", _CERT_CN, str(scratch)], timeout=_ACL_PROBE_TIMEOUT_S
        ).returncode
    if rc != _TIMED_OUT_RC:
        return None
    return (
        f"signing blocks on a GUI authorization prompt -- a test signature with {_CERT_CN!r} "
        f"did not finish within {_ACL_PROBE_TIMEOUT_S:.0f}s, which on a headless host "
        "(SSH / terminal) means macOS is holding up a SecurityAgent dialog nobody can answer"
    )


def _signed_with_stable_cert(app: Path) -> bool:
    """True when `app` already carries a signature from the stable identity.

    `codesign --display` writes the signing chain to stderr; the stable
    certificate is self-signed, so it appears there as the sole `Authority=`
    line, which an ad-hoc signature has none of."""
    proc = _probe(["codesign", "--display", "--verbose=2", str(app)])
    if proc.returncode != 0:
        return False
    return f"Authority={_CERT_CN}" in proc.stderr.decode(errors="replace")


def _expected_dr() -> str:
    """Return the designated requirement pinned to the named identity's SHA-1."""
    listing = _probe(["security", "find-identity", "-p", "codesigning"])
    if listing.returncode != 0:
        detail = listing.stderr.decode(errors="replace").strip() or f"exit {listing.returncode}"
        raise PermissionsHelperBuildError(f"security find-identity failed: {detail}")
    match = _IDENTITY_RE.search(listing.stdout.decode(errors="replace"))
    if match is None:
        raise PermissionsHelperBuildError(
            f"code-signing identity {_CERT_CN!r} is missing or name mismatch prevented "
            "its SHA-1 from being resolved"
        )
    return f'identifier "{_BUNDLE_ID}" and certificate leaf = H"{match.group(1).lower()}"'


def _source_content_hash() -> str:
    """Hash every input that determines the built helper bundle."""
    digest = hashlib.sha256()
    digest.update(_SOURCE.read_bytes())
    digest.update(_INFO_PLIST.read_bytes())
    for path in sorted(p for p in _LOCALES.rglob("*") if p.is_file()):
        digest.update(path.relative_to(_LOCALES).as_posix().encode() + path.read_bytes())
    digest.update(_expected_dr().encode())
    # A signing-policy change is a new artifact, never an in-place re-sign.
    digest.update(" ".join(hardened_runtime.SIGNING_OPTIONS).encode())
    return digest.hexdigest()


def _read_dr(app: Path) -> str:
    proc = _run(["codesign", "-d", "-r-", str(app)])
    # codesign writes the designated-requirement line to stdout on current
    # macOS (stderr carries `Executable=...`); older toolchains have emitted
    # it on stderr, so search both streams rather than pinning one.
    text = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
    match = _DESIGNATED_REQUIREMENT_RE.search(text)
    if match is None:
        raise PermissionsHelperBuildError(
            "codesign did not report a designated requirement for the permissions helper"
        )
    return match.group(1)


def _verify_dr(app: Path) -> str:
    """Verify and return the app's exact designated requirement."""
    actual = _read_dr(app)
    expected = _expected_dr()
    if actual != expected:
        raise PermissionsHelperBuildError(
            "permissions helper designated requirement drift is a macOS permissions reset risk; "
            f"refusing rollout (expected {expected!r}, got {actual!r})"
        )
    return actual


def expected_requirement() -> str:
    """The stable designated requirement every admitted helper must satisfy."""
    return _expected_dr()


def verified_signed_requirement(app: Path) -> str:
    """Return `app`'s designated requirement only if it satisfies the stable identity.

    A finite release-executor job runs the same signed artifact as the home
    helper; admission re-checks the signature instead of trusting a path.
    `codesign --verify` alone accepts code that does not satisfy the requirement
    it embeds (whoever signs chooses that text), so the expected requirement is
    tested cryptographically with `-R`, and the embedded one must also equal it.
    """
    expected = _expected_dr()
    verify = ["codesign", "--verify", "--strict", f"-R={expected}", str(app)]
    if _probe(verify).returncode != 0:
        raise PermissionsHelperBuildError(
            f"signed helper does not satisfy the stable identity requirement: {app}"
        )
    if not _signed_code_flags(app) & hardened_runtime.CS_RUNTIME:
        raise PermissionsHelperBuildError(f"signed helper lacks the hardened runtime: {app}")
    return _verify_dr(app)


def _signed_code_flags(app: Path) -> int:
    proc = _probe(["codesign", "--display", "--verbose=2", str(app)])
    flags = hardened_runtime.code_directory_flags(proc.stderr.decode(errors="replace"))
    if proc.returncode != 0 or flags is None:
        raise PermissionsHelperBuildError(f"cannot read the helper's code-signing flags: {app}")
    return flags


def verified_running_requirement(pid: int, requirement: str) -> None:
    """The running image (not whatever file its path names now) is hardened and satisfies it."""
    if not hardened_runtime.running_hardened(pid):
        raise PermissionsHelperBuildError(
            f"running helper process {pid} lacks a valid hardened runtime; "
            "code injected at its start cannot be excluded"
        )
    if _probe(["codesign", "--verify", f"-R={requirement}", str(pid)]).returncode != 0:
        raise PermissionsHelperBuildError(
            f"running helper process {pid} does not satisfy its signed identity"
        )


def preflight_signing_smoke() -> None:
    """Prove codesign and designated-requirement recovery before a rebuild."""
    refusal = "signing smoke failed — refusing to rebuild/deploy; codesign or keychain unusable"
    try:
        expected_dr = _expected_dr()
        with tempfile.TemporaryDirectory() as td:
            scratch = Path(td) / "signing-smoke"
            scratch.write_bytes(b"\x00")
            try:
                _run(
                    [
                        "codesign",
                        "--sign",
                        _CERT_CN,
                        "-v",
                        "--identifier",
                        _BUNDLE_ID,
                        "--requirements",
                        f"=designated => {expected_dr}",
                        str(scratch),
                    ]
                )
            except PermissionsHelperBuildError as exc:
                detail = str(exc).lower()
                if "-25300" in detail or "errsecitemnotfound" in detail:
                    search_list_note = _signing_keychain_search_list_note()
                    raise PermissionsHelperBuildError(
                        f"{exc}. {_SIGNING_REACH_REMEDY} {search_list_note}"
                    ) from exc
                raise
            actual_dr = _read_dr(scratch)
    except (OSError, PermissionsHelperBuildError) as exc:
        raise PermissionsHelperBuildError(f"{refusal}: {exc}") from exc
    if actual_dr != expected_dr:
        raise PermissionsHelperBuildError(
            f"{refusal}: designated requirement mismatch "
            f"(expected {expected_dr!r}, got {actual_dr!r})"
        )


def _build_state_path() -> Path:
    return _BUILD_DIR / _BUILD_STATE_NAME


def _read_build_state(path: Path | None = None) -> _BuildState | None:
    try:
        raw: object = json.loads((path if path is not None else _build_state_path()).read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    try:
        source_hash = data["source_hash"]
        dr = data["dr"]
        signed_at = data["signed_at"]
    except KeyError:
        return None
    if (
        not isinstance(source_hash, str)
        or not isinstance(dr, str)
        or not isinstance(signed_at, str)
    ):
        return None
    return _BuildState(source_hash=source_hash, dr=dr, signed_at=signed_at)


def _write_build_state(source_hash: str, dr: str, path: Path | None = None) -> None:
    target = path if path is not None else _build_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    state = _BuildState(
        source_hash=source_hash,
        dr=dr,
        signed_at=datetime.now(UTC).isoformat(),
    )
    target.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _app_executable(app: Path) -> Path:
    return app / "Contents" / "MacOS" / "AvaPermissionsHelper"


def _is_valid_stable_app(app: Path) -> bool:
    return (
        _app_executable(app).exists()
        and _probe(["codesign", "--verify", str(app)]).returncode == 0
        and _signed_with_stable_cert(app)
    )


def _remove_app(app: Path) -> None:
    if app.is_symlink() or app.is_file():
        app.unlink(missing_ok=True)
    else:
        shutil.rmtree(app, ignore_errors=True)


def _installed_build_is_current(app: Path, state: _BuildState | None, source_hash: str) -> bool:
    return _is_valid_stable_app(app) and state is not None and state["source_hash"] == source_hash


def ensure_signing_cert() -> None:
    """Provision the stable self-signed code-signing identity if it is missing.

    This is the certificate `build_and_sign` signs with, and the reason a rebuilt
    helper keeps its desktop grants. codesign signs fine with an untrusted
    self-signed cert -- trust only governs Gatekeeper verification, not signing --
    so no trust-settings change (and its GUI auth prompt) is needed. Idempotent:
    a present identity is left as-is."""
    listing = _probe(["security", "find-identity", "-p", "codesigning"])
    if listing.returncode != 0:
        raise PermissionsHelperBuildError(
            f"security find-identity failed: {listing.stderr.decode(errors='replace').strip()}"
        )
    if _CERT_CN in listing.stdout.decode(errors="replace"):
        return
    keychain = str(_keychain_path())
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        key, cert, p12 = d / "key.pem", d / "cert.pem", d / "ident.p12"
        _run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "3650",
                "-nodes",
                "-subj",
                f"/CN={_CERT_CN}",
                "-addext",
                "basicConstraints=critical,CA:false",
                "-addext",
                "keyUsage=critical,digitalSignature",
                "-addext",
                "extendedKeyUsage=critical,codeSigning",
            ]
        )
        _run(
            [
                "openssl",
                "pkcs12",
                "-export",
                "-inkey",
                str(key),
                "-in",
                str(cert),
                "-out",
                str(p12),
                "-passout",
                "pass:ava",
                "-name",
                _CERT_CN,
            ]
        )
        _run(
            [
                "security",
                "import",
                str(p12),
                "-k",
                keychain,
                "-P",
                "ava",
                "-T",
                "/usr/bin/codesign",
                "-A",
            ]
        )


def _build_directory(destination: Path | None) -> Path:
    """Choose an immutable artifact destination outside the installed bundle tree."""
    from shared.cluster import default_home

    build_dir = _BUILD_DIR if destination is None else destination
    protected = {_BUILD_DIR.resolve(), (default_home() / "helper").resolve()}
    if destination is not None and (
        not destination.is_absolute()
        or destination.resolve() != destination
        or any(
            destination == path or path in destination.parents or destination in path.parents
            for path in protected
        )
    ):
        raise PermissionsHelperBuildError(
            "isolated helper destination must be canonical and outside the installed bundle directory"
        )
    return build_dir


def build_and_sign(*, destination: Path | None = None) -> tuple[Path, bool]:
    """Compile and sign the helper; return (app bundle path, rebuilt).

    Skips the compile + sign only when the installed bundle is valid and its
    recorded content hash matches all source and identity inputs. Checkout
    mtimes therefore cannot churn the binary or its TCC identity."""
    build_dir = _build_directory(destination)
    state_path = build_dir / _BUILD_STATE_NAME
    app = build_dir / "AvaPermissionsHelper.app"
    exe = _app_executable(app)
    source_hash = _source_content_hash()
    expected_dr = _expected_dr()
    state = _read_build_state(state_path)
    if state is not None and state["dr"] != expected_dr:
        sys.stderr.write(
            "  ! permissions-helper: code-signing identity changed — "
            "macOS permissions may need re-granting\n"
        )

    if _installed_build_is_current(app, state, source_hash):
        return app, False
    if _app_executable(app).exists():
        raise PermissionsHelperBuildError(
            "signed helper artifacts are immutable; prepare a new AVA_PERMISSIONS_HELPER_ARTIFACT_DIR "
            "and activate it after the home-specific helper job has been stopped and unregistered"
        )

    # Only a real rebuild needs the signing key, so neither check below can fail a
    # converge on a host whose helper is already current -- the common SSH case.
    # Both run before anything is compiled or written, so a host that cannot sign
    # is turned away without a half-built bundle left behind.
    locked = _keychain_lock_reason()
    if locked is not None:
        raise PermissionsHelperSigningUnavailableError(
            f"cannot sign the permissions helper as {_CERT_CN!r}: {locked}. {_AD_HOC_REFUSAL}"
        )
    blocked = _interactive_signing_reason()
    if blocked is not None:
        raise PermissionsHelperSigningUnavailableError(
            f"cannot sign the permissions helper as {_CERT_CN!r}: {blocked}. {_ACL_REMEDY}"
        )
    preflight_signing_smoke()

    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / "AvaPermissionsHelper"
    _run(["swiftc", "-O", str(_SOURCE), "-o", str(binary)])

    _remove_app(app)
    exe.parent.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(_INFO_PLIST.read_bytes())
    exe.write_bytes(binary.read_bytes())
    exe.chmod(0o755)
    for lproj in sorted(_LOCALES.glob("*.lproj")):
        shutil.copytree(lproj, app / "Contents" / "Resources" / lproj.name, dirs_exist_ok=True)

    try:
        _run(
            [
                "codesign",
                "--force",
                "--sign",
                _CERT_CN,
                *hardened_runtime.SIGNING_OPTIONS,
                "--identifier",
                _BUNDLE_ID,
                "--requirements",
                f"=designated => {expected_dr}",
                str(app),
            ]
        )
    except PermissionsHelperTimeoutError as exc:
        # The preflight cleared this host, so a sign that still ran out the clock
        # is the same GUI prompt arriving late (a key ACL that grants per-item, a
        # keychain relocked mid-converge) -- point at the ACL, not at the unlock.
        raise PermissionsHelperBuildError(f"{exc}. {_ACL_REMEDY}") from exc
    except PermissionsHelperBuildError as exc:
        raise PermissionsHelperBuildError(f"{exc}. {_AD_HOC_REFUSAL}") from exc
    actual_dr = _verify_dr(app)
    _write_build_state(source_hash, actual_dr, state_path)
    return app, True


def _label() -> str:
    return helper_job_label()


def _agents_dir() -> Path:
    return helper_job_agents_dir()


def _plist_path() -> Path:
    return helper_job_plist_path()


def _domain() -> str:
    return helper_job_domain()


def _is_loaded() -> bool:
    from services.permissions_helper.launchd_job import _retirement_query

    return _retirement_query(f"{_domain()}/{_label()}", time.monotonic() + 30.0) is not None


def _stale_plists() -> list[Path]:
    """LaunchAgent plists bound to this cluster's socket under a foreign label.

    A job whose plist pins this cluster's socket but carries a label other than
    the current one is a leftover racing this cluster's own job for the same
    socket; which one a client reaches then depends on the last bind. Other
    clusters' jobs are left alone -- they pin their own sockets."""
    sock = str(permissions_helper_socket())
    stale: list[Path] = []
    for plist in _agents_dir().glob(f"{_BUNDLE_ID}.*.plist"):
        try:
            data = plistlib.loads(plist.read_bytes())
        except (plistlib.InvalidFileException, OSError):
            continue
        if data.get("Label") == _label():
            continue
        env: dict[str, object] = data.get("EnvironmentVariables") or {}
        if env.get("AVA_PERMISSIONS_HELPER_SOCKET") == sock:
            stale.append(plist)
    return stale


def _refuse_stale_jobs() -> None:
    stale = _stale_plists()
    if stale:
        raise PermissionsHelperBuildError(
            "foreign helper labels bind this home's socket; reconcile them externally before start: "
            + ", ".join(str(path) for path in stale)
        )


def install_and_load(app: Path, *, rebuilt: bool) -> None:
    """Write the LaunchAgent plist and ensure launchd is running this binary.

    A loaded helper is only observed. Replacement requires an external stop
    and exact-home unregister first, so a descendant cannot unload its parent."""
    _refuse_stale_jobs()
    exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
    log = logs_dir() / "permissions-helper.log"
    plist = {
        "Label": _label(),
        "ProgramArguments": [str(exe)],
        "EnvironmentVariables": {
            "AVA_PERMISSIONS_HELPER_SOCKET": str(permissions_helper_socket()),
            "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(shared.paths.root_run_dir() / "seed.json"),
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = plistlib.dumps(plist)
    plist_changed = not path.exists() or path.read_bytes() != new_bytes
    loaded = _is_loaded()
    if loaded and helper_stop_intent(shared.paths.ava_home()):
        raise PermissionsHelperBuildError(
            "helper retirement is incomplete; finish ava stop externally before starting"
        )
    if loaded and (plist_changed or rebuilt):
        raise PermissionsHelperBuildError(
            "loaded helper differs from the requested artifact or job; stop and unregister "
            "this home's helper from outside its process tree before activating the replacement"
        )
    if not loaded:
        shared.paths.root_run_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        clear_helper_stop_intent(shared.paths.ava_home())
        path.write_bytes(new_bytes)
        _run(["launchctl", "bootstrap", _domain(), str(path)])
    if not _helper_answers_ping():
        raise PermissionsHelperBuildError(
            "permissions helper is unresponsive; custody is unknown. Diagnose the native job "
            "externally; start will not bootout or kickstart its possible ancestor"
        )


def _helper_answers_ping() -> bool:
    from services.permissions_helper import client

    for attempt in range(_HELPER_PING_ATTEMPTS):
        try:
            reply = client.ping()
        except Exception:
            reply = None
        if reply is not None and reply.get("pong") is True:
            if (
                reply.get("root_stop_intent_v1") is not True
                or reply.get("helper_shutdown_v1") is not True
            ):
                raise PermissionsHelperBuildError(
                    "helper lacks the durable stop/shutdown protocol; upgrade its signed artifact "
                    "after external exact-home retirement before starting"
                )
            return True
        if attempt < _HELPER_PING_ATTEMPTS - 1:
            time.sleep(_HELPER_PING_SETTLE_S)
    return False


def _require_usable_socket_paths() -> None:
    """Refuse impossible macOS IPC names before signing or registering jobs."""
    paths = (permissions_helper_socket(), shared.paths.root_run_dir() / "ava-root.sock")
    for path in paths:
        # Darwin sockaddr_un.sun_path has 104 bytes, including the terminating NUL.
        if len(os.fsencode(path)) >= 104:
            raise PermissionsHelperBuildError(
                f"macOS IPC path exceeds 103 bytes: {path}; choose a shorter cluster home"
            )


def converge() -> None:
    """Idempotent full bring-up: cert, build+sign, load. Raises on any failure."""
    _require_usable_socket_paths()
    ensure_signing_cert()
    app, rebuilt = build_and_sign(destination=settings.services.permissions_helper_artifact_dir)
    install_and_load(app, rebuilt=rebuilt)
