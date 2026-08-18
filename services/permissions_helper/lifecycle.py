"""Build, sign, and launchd-manage the macOS permissions helper.

This is the bring-up the converge phase runs on an agent-runner. It exists
because the helper has a hard requirement the session-supervised services do not:
to hold its own Screen Recording / Accessibility grants it must be launched by
launchd (so it is its own responsible process, not a child of the terminal that
borrows the terminal's grants), and signed by a STABLE certificate (so the grant
is keyed on an identity that survives every rebuild). The steps, all idempotent:

  1. ensure a stable self-signed code-signing certificate in the login keychain
  2. compile main.swift and codesign the .app bundle with that certificate
  3. write a per-cluster LaunchAgent plist and (re)load it under launchd
  4. retire any old-layout helper job still bound to this cluster's socket

Granting the helper its desktop permissions once, in System Settings, is a
manual one-time operator step (the OS gates it behind a human); after that the
stable identity means rebuilds never re-prompt.

Step 4 exists because the launchd label used to be the fixed
``com.ava.permissions-helper.main``; once labels became per-cluster home slugs,
converge wrote ``com.ava.permissions-helper.<home-slug>`` but nothing removed a
``main`` job already loaded on a host. Two KeepAlive jobs then raced for the
same socket and which one a client reached depended on the last bind. Any
loaded plist pinning this cluster's socket under a different label is that
leftover, so it is booted out and deleted.

Ad-hoc signing (`codesign --sign -`) is never a substitute for step 1's
certificate: it mints a throwaway identity per build, so TCC stops recognizing
the helper and the operator has to re-authorize by hand every time. The one
situation that tempts it -- a locked login keychain, the norm over SSH -- is
therefore reported as a build failure naming the unlock, not signed around.

Every step shells out to a fixed system tool, and every one of those calls is
bounded (`_TIMEOUTS_S`). Converge runs headless, so a tool that stops to ask a
human is a hang with no answer coming rather than a slow success -- and the two
prompts that reach for a human, a locked keychain and a key whose access control
demands confirmation, are both checked for before any signing starts.
"""

from __future__ import annotations

import plistlib
import subprocess
import tempfile
from pathlib import Path

from shared.config import settings
from shared.paths import logs_dir, permissions_helper_socket
from shared.proc import run_bounded

_CERT_CN = "Ava Permissions Helper Code Signing"
_BUNDLE_ID = "com.ava.permissions-helper"  # fixed across clusters so one grant covers all
_SERVICE_DIR = Path(__file__).resolve().parent
_SOURCE = _SERVICE_DIR / "helper" / "main.swift"
_INFO_PLIST = _SERVICE_DIR / "helper" / "Info.plist"
_BUILD_DIR = _SERVICE_DIR / "build"
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
# Same discipline `cli/commands/_update_git.py` applies to git.
_TIMEOUTS_S = {
    "swiftc": 300.0,  # one small file; the ceiling only catches a wedged toolchain
    "codesign": 60.0,  # signs a few-MB bundle in well under a second
    "security": 30.0,  # keychain queries and a PKCS#12 import, all local
    "openssl": 60.0,  # RSA-2048 keygen + export; slow only on a starved entropy pool
    "launchctl": 30.0,  # local IPC with launchd
    "id": 10.0,  # reads the uid
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
    return Path.home() / "Library" / "Keychains" / "login.keychain-db"


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


def build_and_sign() -> tuple[Path, bool]:
    """Compile and sign the helper; return (app bundle path, rebuilt).

    Skips the compile + sign when the signed executable is already newer than
    the source, so a converge that changed nothing does not churn the binary
    (a fresh cdhash) or bounce the running helper. `rebuilt` is False on a skip."""
    app = _BUILD_DIR / "AvaPermissionsHelper.app"
    exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
    if exe.exists():
        newest_src = max(_SOURCE.stat().st_mtime, _INFO_PLIST.stat().st_mtime)
        # "Up to date" means built AND signed by the stable identity. codesign runs
        # after the exe is written, so a fresh mtime alone could be a half-built,
        # unsigned binary from a prior failed sign; and a bundle an older build left
        # ad-hoc-signed holds a throwaway identity that TCC drops on every rebuild
        # anyway, so re-signing it forfeits no live grant and is the only way it
        # returns to the stable one. Either case rebuilds rather than loads.
        verified = _probe(["codesign", "--verify", str(app)]).returncode == 0
        if verified and _signed_with_stable_cert(app) and exe.stat().st_mtime >= newest_src:
            return app, False

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

    _BUILD_DIR.mkdir(parents=True, exist_ok=True)
    binary = _BUILD_DIR / "AvaPermissionsHelper"
    _run(["swiftc", "-O", str(_SOURCE), "-o", str(binary)])

    exe.parent.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_bytes(_INFO_PLIST.read_bytes())
    exe.write_bytes(binary.read_bytes())
    exe.chmod(0o755)

    try:
        _run(["codesign", "--force", "--sign", _CERT_CN, "--identifier", _BUNDLE_ID, str(app)])
    except PermissionsHelperTimeoutError as exc:
        # The preflight cleared this host, so a sign that still ran out the clock
        # is the same GUI prompt arriving late (a key ACL that grants per-item, a
        # keychain relocked mid-converge) -- point at the ACL, not at the unlock.
        raise PermissionsHelperBuildError(f"{exc}. {_ACL_REMEDY}") from exc
    except PermissionsHelperBuildError as exc:
        raise PermissionsHelperBuildError(f"{exc}. {_AD_HOC_REFUSAL}") from exc
    return app, True


def _label() -> str:
    # Per-cluster job keyed on the home-path slug (path-only identity); the
    # bundle id (the TCC grant) stays shared across clusters.
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return f"{_BUNDLE_ID}.{home_slug(ava_home())}"


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path() -> Path:
    return _agents_dir() / f"{_label()}.plist"


def _domain() -> str:
    uid = _run(["id", "-u"]).stdout.decode().strip()
    return f"gui/{uid}"


def _is_loaded() -> bool:
    return _probe(["launchctl", "print", f"{_domain()}/{_label()}"]).returncode == 0


def _stale_plists() -> list[Path]:
    """LaunchAgent plists bound to this cluster's socket under a foreign label.

    A job whose plist pins this cluster's socket (current or pre-rename name)
    but carries a label other than the current one is a leftover racing this
    cluster's own job for the same socket; which one a client reaches then
    depends on the last bind. Two generations match: the old-layout fixed
    ``com.ava.permissions-helper.main`` era (and the pre-rename
    ``com.ava.native-helper.*`` labels, whose plists pin the old socket file
    name + env key -- same port, derived above). Other clusters' jobs are left
    alone -- they pin their own sockets."""
    sock = str(permissions_helper_socket())
    # The pre-rename job bound this cluster's socket under the OLD socket file
    # name (`native-helper.<port>.sock`) and env key; same port, so it is
    # derivable and matched the same way.
    from shared.paths import run_dir

    legacy_sock = str(run_dir() / f"native-helper.{settings.services.permissions_helper_port}.sock")
    stale: list[Path] = []
    for plist in list(_agents_dir().glob(f"{_BUNDLE_ID}.*.plist")) + list(
        _agents_dir().glob("com.ava.native-helper.*.plist")
    ):
        try:
            data = plistlib.loads(plist.read_bytes())
        except (plistlib.InvalidFileException, OSError):
            continue
        if data.get("Label") == _label():
            continue
        env: dict[str, object] = data.get("EnvironmentVariables") or {}
        if (
            env.get("AVA_PERMISSIONS_HELPER_SOCKET") == sock
            or env.get("AVA_NATIVE_HELPER_SOCKET") == legacy_sock
        ):
            stale.append(plist)
    return stale


def _retire_stale_jobs() -> None:
    """Boot out and delete old-layout helper jobs bound to this socket.

    Idempotent: bootout is best-effort (the job may already be gone) and the
    plist deletion is the durable step."""
    domain = _domain()
    for plist in _stale_plists():
        try:
            label = plistlib.loads(plist.read_bytes())["Label"]
        except (plistlib.InvalidFileException, OSError):
            continue
        _probe(["launchctl", "bootout", f"{domain}/{label}"])
        plist.unlink(missing_ok=True)
    # A booted-out job leaves its socket file behind; drop the pre-rename name so
    # a stale dead socket never shadows the live one.
    from shared.paths import run_dir

    Path(run_dir() / f"native-helper.{settings.services.permissions_helper_port}.sock").unlink(
        missing_ok=True
    )


def install_and_load(app: Path, *, rebuilt: bool) -> None:
    """Write the LaunchAgent plist and ensure launchd is running this binary.

    Bootstraps the job when it is not loaded; kickstarts (bounces) it only when
    the binary was rebuilt, so a no-op converge leaves a healthy helper alone.
    Old-layout jobs racing this cluster's socket are retired first."""
    _retire_stale_jobs()
    exe = app / "Contents" / "MacOS" / "AvaPermissionsHelper"
    log = logs_dir() / "permissions-helper.log"
    plist = {
        "Label": _label(),
        "ProgramArguments": [str(exe)],
        "EnvironmentVariables": {"AVA_PERMISSIONS_HELPER_SOCKET": str(permissions_helper_socket())},
        "RunAtLoad": True,
        "KeepAlive": True,  # launchd respawns the helper if it crashes
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = plistlib.dumps(plist)
    plist_changed = not path.exists() or path.read_bytes() != new_bytes
    path.write_bytes(new_bytes)

    loaded = _is_loaded()
    if loaded and plist_changed:
        # kickstart restarts the process but reuses launchd's in-memory job
        # definition; a changed plist (socket / log / env) only takes effect on a
        # bootout + bootstrap.
        _probe(["launchctl", "bootout", f"{_domain()}/{_label()}"])
        loaded = False
    if not loaded:
        _run(["launchctl", "bootstrap", _domain(), str(path)])
    elif rebuilt:
        _run(["launchctl", "kickstart", "-k", f"{_domain()}/{_label()}"])


def converge() -> None:
    """Idempotent full bring-up: cert, build+sign, load. Raises on any failure."""
    ensure_signing_cert()
    app, rebuilt = build_and_sign()
    install_and_load(app, rebuilt=rebuilt)
