"""Client for the macOS permissions helper daemon.

Connects to this cluster's helper and exchanges one line-delimited JSON
request/response per call over a Unix socket. Skills that drive the macOS
desktop (screen capture, clicks, keystrokes, window geometry) call these
functions instead of shelling out to screencapture / posting CGEvents
themselves -- so the privileged, permission-granted work happens in the one
signed helper process, not in every caller.

Because the helper is that single process, it is also the only place the
desktop permission grants can be read from; `check_screen_capture` and
`check_accessibility` are the interpreted calls here, turning a `ping` into the
statuses the converge preflight reports.
"""

from __future__ import annotations

import base64
import binascii
import itertools
import json
import socket
import time
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from base.host.converge.accessibility import AccessibilityState, AccessibilityStatus
from base.host.converge.screen_capture import ScreenCaptureState, ScreenCaptureStatus
from base.host.net.resilience import Policy, retry
from base.paths import permissions_helper_socket

_LINE_LIMIT = (
    64 * 1024 * 1024
)  # client-side cap on one response line; metadata stays small but keep headroom
_CONNECT_ATTEMPTS = 5
_CONNECT_DELAY_S = 0.2
_CALL_TIMEOUT_S = (
    30.0  # bound a connected read so a stalled helper surfaces as an error, not a hang
)
# A helper launchd has only just bootstrapped may not have bound its socket yet.
# Keep retrying the preflight ping for this long so a cold start is not reported
# as a dead helper -- a false alarm is the exact failure this probe replaced.
_PROBE_SETTLE_S = 5.0
_PROBE_RETRY_DELAY_S = 0.25  # a ping can also fail instantly, so pace the retry
_ids = itertools.count(1)


class PermissionsHelperError(RuntimeError):
    """A permissions helper call failed, or the daemon was unreachable."""


def connect(path: str) -> socket.socket:
    """Open the helper's Unix socket, retrying briefly while it is absent or refusing."""
    phase = ["socket"]
    policy = Policy(
        max_attempts=_CONNECT_ATTEMPTS,
        backoff=lambda attempt: _CONNECT_DELAY_S,  # noqa: ARG005 — Backoff keyword name
        jitter="none",
        jitter_span=1.0,
        classify=lambda exc: (
            phase[0] == "connect" and isinstance(exc, (FileNotFoundError, ConnectionRefusedError))
        ),
        idempotent=True,
        respect_retry_after=False,
        on_final_failure=None,
    )

    def once() -> socket.socket:
        phase[0] = "socket"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        phase[0] = "connect"
        try:
            s.connect(path)
            return s
        except PermissionError:
            # Not an unreachability signal — the socket exists and answers, but
            # this caller's ACLs are wrong. Surface it raw instead of closing
            # the socket and relabeling it "not reachable": that message would
            # send an operator chasing a dead helper instead of a permissions fix.
            raise
        except OSError:
            phase[0] = "close"
            s.close()
            phase[0] = "connect"
            raise

    last: OSError | None = None
    try:
        return retry(policy)(once)
    except PermissionError:
        raise
    except OSError as exc:
        if phase[0] != "connect":
            raise
        last = exc
    raise PermissionsHelperError(f"permissions helper not reachable at {path}: {last}")


def _call(
    method: str,
    *,
    sock_path: str | Path | None = None,
    **args: object,
) -> Any:
    """One JSON-line request/response over this cluster's Unix socket."""
    req = {"id": next(_ids), "method": method, **args}
    path = str(sock_path or permissions_helper_socket())
    return _exchange(connect(path), method, req)


def _exchange(s: socket.socket, method: str, req: dict[str, object]) -> Any:
    """Send one request line on a connected socket, read one reply, close it."""
    s.settimeout(_CALL_TIMEOUT_S)
    try:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
            if len(buf) > _LINE_LIMIT:
                raise PermissionsHelperError("permissions helper response exceeded line limit")
    except TimeoutError as e:
        raise PermissionsHelperError(
            f"permissions helper did not respond to {method!r} within {_CALL_TIMEOUT_S}s"
        ) from e
    finally:
        s.close()
    return parse_reply(bytes(buf), method)


def parse_reply(buf: bytes, method: str) -> Any:
    """The helper's JSON-line reply contract."""
    if not buf:
        raise PermissionsHelperError(f"permissions helper closed without a response to {method!r}")
    if not buf.endswith(b"\n"):
        raise PermissionsHelperError(f"permissions helper response to {method!r} was truncated")
    resp = json.loads(buf)
    # ok / error / result are contract-guaranteed by the daemon's dispatch; index
    # with [] so a wire-format break blows up here instead of being masked.
    if not resp["ok"]:
        raise PermissionsHelperError(resp["error"])
    return resp["result"]


# Per-method `result` shapes. Each mirrors the object the Swift daemon's matching
# handler returns (`services/permissions_helper/helper/main.swift`); they are the typed face
# of `_call`'s dynamic JSON so callers index fields, not a bare dict.


class PingResult(TypedDict):
    pong: bool
    pid: NotRequired[int]
    root_stop_intent_v1: NotRequired[bool]
    helper_shutdown_v1: NotRequired[bool]
    root_seed_report_v1: NotRequired[bool]  # `root_status.seed` is reported
    preflight_screen: bool  # Screen Recording grant held
    ax_trusted: bool  # Accessibility grant held
    ax_tree_v1: NotRequired[bool]  # the helper serves `ax_tree`


class ScreencaptureResult(TypedDict):
    path: str
    bytes: int  # PNG size on disk, or -1 if it could not be stat'd


class ClickPoint(TypedDict):
    x: float
    y: float


class ClickResult(TypedDict):
    clicked: ClickPoint
    double: bool


class TypeResult(TypedDict):
    typed: int  # characters sent


class KeyResult(TypedDict):
    key: int  # the virtual keycode pressed
    cmd: bool


class ScrollResult(TypedDict):
    scrolled: int  # dy pixels applied


class WindowGeometry(TypedDict):
    x: float
    y: float
    w: float
    h: float


class AxWindowInfo(WindowGeometry):
    app: str


class WindowInfo(WindowGeometry):
    owner: str


class AxNode(TypedDict):
    """One raw accessibility element; geometry is logical points. Absent keys
    mean the app did not expose that attribute."""

    id: int
    depth: int
    n: int  # children the app listed (visible ones for list-like roles)
    parent: NotRequired[int]
    role: NotRequired[str]
    subrole: NotRequired[str]
    title: NotRequired[str]
    desc: NotRequired[str]
    value: NotRequired[str]
    ident: NotRequired[str]
    x: NotRequired[float]
    y: NotRequired[float]
    w: NotRequired[float]
    h: NotRequired[float]
    enabled: NotRequired[bool]
    focused: NotRequired[bool]
    selected: NotRequired[bool]
    actions: NotRequired[list[str]]


class AxTreeResult(TypedDict):
    app: str
    pid: int
    windows: int
    framework: str  # "electron" / "cef" when the bundle ships one, else ""
    nodes: list[AxNode]
    visited: int
    truncated: bool  # the node or depth cap cut the walk
    timed_out: bool  # the time budget cut the walk
    unreadable: int  # elements whose attributes could not be read
    elapsed_ms: int


class SessionInfo(TypedDict):
    locked: bool
    on_console: bool


class SessionProc(TypedDict):
    name: str
    pid: int
    alive: bool


class SpawnResult(TypedDict):
    pid: int
    reused: bool


class SessionListResult(TypedDict):
    sessions: list[SessionProc]


class AliveResult(TypedDict):
    alive: bool


class SignalResult(TypedDict):
    sent: bool


class RootSeedConfig(TypedDict):
    """The root keeper's launch config (wire `root_seed.config`).

    All paths absolute; `env` entries override the helper's own environment
    for the root process.
    """

    argv: list[str]
    cwd: str
    run_dir: str
    stdout: str
    stderr: str
    env: NotRequired[dict[str, str]]


class RootSeedReport(TypedDict):
    """The seed the keeper holds for its next spawn; its environment is withheld."""

    argv: list[str]
    cwd: str
    run_dir: str
    stdout: str
    stderr: str


class RootExitInfo(TypedDict):
    """How the root process last ended (a `last_exit` on `RootStatus`)."""

    kind: str  # clean | refused | crash | stopped | spawn-failed
    at: float
    code: NotRequired[int]
    signal: NotRequired[int]
    detail: NotRequired[str]


class RootConflictInfo(TypedDict):
    """A live root that holds the run dir but was not seeded by this helper."""

    pid: NotRequired[int]  # omitted when the lock's pid line was unreadable
    since: NotRequired[float]


class RootStatus(TypedDict):
    """The root keeper's state (wire `root_status`, and every mutating reply)."""

    state: str  # unseeded | running | backoff | conflict | stopping | stopped
    seeded: bool
    restarts: int
    stop_requested: bool
    run_dir: NotRequired[str]
    seed: NotRequired[RootSeedReport]  # present while seeded (`root_seed_report_v1`)
    pid: NotRequired[int]  # the keeper's live root child
    last_exit: NotRequired[RootExitInfo]
    next_restart_in_s: NotRequired[float]
    conflict: NotRequired[RootConflictInfo]
    seed_error: NotRequired[str]  # startup seed file was rejected


class HelperShutdownResult(TypedDict):
    stopping: bool
    pid: int
    run_dir: str


class ScreenSize(TypedDict):
    x: float
    y: float
    w: float
    h: float
    scale: float  # backing scale factor


class FrontmostApp(TypedDict):
    app: str  # display name, or "" when nothing is focused


class PermissionsFileEntry(TypedDict):
    name: str
    size: int
    mtime: int
    is_dir: bool


class FileListResult(TypedDict):
    entries: list[PermissionsFileEntry]


class FileReadResult(TypedDict):
    content_b64: str


def ping(*, sock_path: str | Path | None = None) -> PingResult:
    """Report the helper's liveness and whether it holds the desktop grants."""
    return _call("ping", sock_path=sock_path)


def screencapture_region(
    x: int, y: int, w: int, h: int, path: str, *, sock_path: str | Path | None = None
) -> ScreencaptureResult:
    """Capture the screen rectangle (x, y, w, h) to a PNG at `path`."""
    return _call("screencapture_region", x=x, y=y, w=w, h=h, path=path, sock_path=sock_path)


def list_dir(path: str, *, sock_path: str | Path | None = None) -> list[PermissionsFileEntry]:
    """List a whitelisted directory's immediate entries, sorted by name."""
    result: FileListResult = _call("file_list", path=path, sock_path=sock_path)
    return result["entries"]


def read_file(path: str, *, sock_path: str | Path | None = None) -> bytes:
    """Read a whitelisted regular file of at most 32 MiB."""
    result: FileReadResult = _call("file_read", path=path, sock_path=sock_path)
    try:
        content_b64 = result["content_b64"]
    except (KeyError, TypeError) as exc:
        raise PermissionsHelperError("invalid file_read response") from exc
    if not isinstance(content_b64, str):
        raise PermissionsHelperError("invalid file_read response")
    try:
        content = base64.b64decode(content_b64, validate=False)
    except (ValueError, binascii.Error) as exc:
        raise PermissionsHelperError("invalid file_read response") from exc
    if base64.b64encode(content).decode("ascii") != content_b64:
        raise PermissionsHelperError("invalid file_read response")
    return content


def click(
    x: float, y: float, *, double: bool = False, sock_path: str | Path | None = None
) -> ClickResult:
    """Click the left mouse button at the global screen point (x, y)."""
    return _call("click", x=x, y=y, double=double, sock_path=sock_path)


def type_text(text: str, *, sock_path: str | Path | None = None) -> TypeResult:
    """Type `text` as keyboard input into the focused field (handles CJK)."""
    return _call("type", text=text, sock_path=sock_path)


def key(code: int, *, cmd: bool = False, sock_path: str | Path | None = None) -> KeyResult:
    """Press the key with virtual keycode `code`."""
    return _call("key", code=code, cmd=cmd, sock_path=sock_path)


def scroll(x: float, y: float, dy: int, *, sock_path: str | Path | None = None) -> ScrollResult:
    """Move to (x, y) and scroll vertically by `dy` pixels (negative = older)."""
    return _call("scroll", x=x, y=y, dy=dy, sock_path=sock_path)


def ax_window_info(app: str, *, sock_path: str | Path | None = None) -> AxWindowInfo:
    """Report the on-screen geometry of `app`'s focused window via accessibility."""
    return _call("ax_window_info", app=app, sock_path=sock_path)


def ax_tree(
    app: str,
    *,
    scope: str | None = None,
    max_nodes: int = 600,
    max_depth: int = 14,
    budget_ms: int = 1500,
    timeout_ms: int = 400,
    sock_path: str | Path | None = None,
) -> AxTreeResult:
    """Read `app`'s focused-window accessibility tree (or the subtree under a
    `scope` id from the last walk), bounded by node, depth and time limits."""
    req: dict[str, object] = {
        "max_nodes": max_nodes,
        "max_depth": max_depth,
        "budget_ms": budget_ms,
        "timeout_ms": timeout_ms,
    }
    if scope is not None:
        req["scope"] = scope
    return _call("ax_tree", app=app, sock_path=sock_path, **req)


def window_info(owner: str, *, sock_path: str | Path | None = None) -> WindowInfo:
    """Report the geometry of `owner`'s normal on-screen window via the window list."""
    return _call("window_info", owner=owner, sock_path=sock_path)


def session_info(*, sock_path: str | Path | None = None) -> SessionInfo:
    """Report whether the login session is locked or off-console."""
    return _call("session_info", sock_path=sock_path)


def spawn_process(
    name: str,
    argv: list[str],
    env: dict[str, str],
    cwd: str,
    stdout: str,
    stderr: str,
    *,
    sock_path: str | Path | None = None,
) -> SpawnResult:
    """Spawn or reuse a named direct child of the permissions helper."""
    result: SpawnResult = _call(
        "spawn",
        name=name,
        argv=argv,
        env=env,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        sock_path=sock_path,
    )
    return result


def session_list(prefix: str = "", *, sock_path: str | Path | None = None) -> list[SessionProc]:
    """List helper-owned process sessions whose names start with `prefix`."""
    result: SessionListResult = _call("session_list", prefix=prefix, sock_path=sock_path)
    return result["sessions"]


def session_has(name: str, *, sock_path: str | Path | None = None) -> bool:
    """Report whether the named helper-owned process session is alive."""
    result: AliveResult = _call("session_has", name=name, sock_path=sock_path)
    return result["alive"]


def signal_session(
    *,
    name: str | None = None,
    pid: int | None = None,
    sig: int = 15,
    sock_path: str | Path | None = None,
) -> bool:
    """Send `sig` to exactly one named session or explicit pid."""
    if (name is None) == (pid is None):
        raise ValueError("exactly one of name or pid is required")
    if name is not None:
        result: SignalResult = _call("signal", name=name, sig=sig, sock_path=sock_path)
    else:
        result = _call("signal", pid=pid, sig=sig, sock_path=sock_path)
    return result["sent"]


def seed_root(config: RootSeedConfig, *, sock_path: str | Path | None = None) -> RootStatus:
    """Tell the root keeper what ava-root to launch and keep alive.

    The seed never disrupts a live root it already started — it applies to the
    next spawn. A run dir already held by another live root leaves the keeper
    in `conflict`: nothing is spawned, nothing is killed.
    """
    result: RootStatus = _call("root_seed", config=config, sock_path=sock_path)
    return result


def root_status(*, sock_path: str | Path | None = None) -> RootStatus:
    """Report the root keeper's state (works even when no seed is configured)."""
    result: RootStatus = _call("root_status", sock_path=sock_path)
    return result


def stop_root(*, sock_path: str | Path | None = None) -> RootStatus:
    """Durably stop the owned root; foreign or unknown custody always refuses."""
    result: RootStatus = _call("root_stop", sock_path=sock_path)
    return result


def shutdown_helper(run_dir: Path, *, sock_path: str | Path) -> HelperShutdownResult:
    """Close this home's native admission, retain intent, and request exit zero."""
    return _call("helper_shutdown", run_dir=str(run_dir), sock_path=sock_path)


def screen_size(*, sock_path: str | Path | None = None) -> ScreenSize:
    """Report the main display's geometry in logical points + backing scale.

    Computer-use callers use this to map screenshot pixels (physical) to
    click coordinates (logical): divide by `scale`."""
    return _call("screen_size", sock_path=sock_path)


def frontmost_app(*, sock_path: str | Path | None = None) -> FrontmostApp:
    """Report the frontmost application's display name ("" when none)."""
    return _call("frontmost_app", sock_path=sock_path)


_NO_GRANT_DIAGNOSTIC = (
    "The permissions helper is running but holds no Screen Recording grant, so OS-level "
    "screen capture returns the wallpaper or a black image. This affects skills that "
    "capture the macOS desktop through the helper; browser screenshots are taken over "
    "Chrome DevTools and are unaffected. Fix: System Settings > Privacy & Security > "
    "Screen Recording, enable AvaPermissionsHelper, then restart its launchd job "
    "(`launchctl list | grep com.ava.permissions-helper` for the label, then "
    "`launchctl kickstart -k gui/$(id -u)/<label>`)."
)

_NO_AX_GRANT_DIAGNOSTIC = (
    "The permissions helper is running but holds no Accessibility grant, so macOS silently "
    "drops the synthetic clicks and keystrokes it posts (calls return success but the desktop "
    "never sees them). Screen Recording is a separate grant and is unaffected. Fix: System "
    "Settings > Privacy & Security > Accessibility, enable AvaPermissionsHelper — rebuilding "
    "or re-signing the helper resets this grant once. The grant applies to the running helper "
    "immediately; no launchd restart is needed."
)


def check_screen_capture(
    *, sock_path: str | Path | None = None, settle_s: float = _PROBE_SETTLE_S
) -> ScreenCaptureStatus:
    """Report whether OS-level screen capture works, by asking the helper.

    The grant that decides this belongs to the helper, since the helper is the
    process that runs `screencapture_region`. Preflighting the calling process
    instead would report the grant it inherited from whatever started it, which
    for a terminal session started over SSH is none -- a permanent false alarm on a
    host whose helper is perfectly authorized.

    A helper that never answers gets its own state rather than being folded into
    "no permission": the grant was not read, so claiming it is missing would be
    a guess, and the fix is a launchd one, not a System Settings one.
    """
    deadline = time.monotonic() + settle_s
    while True:
        try:
            result = ping(sock_path=sock_path)
        except PermissionsHelperError as exc:
            if time.monotonic() < deadline:
                time.sleep(_PROBE_RETRY_DELAY_S)
                continue
            return ScreenCaptureStatus(
                state=ScreenCaptureState.HELPER_UNREACHABLE,
                diagnostic=(
                    f"The permissions helper did not answer on its socket ({exc}), so its "
                    "Screen Recording grant could not be read -- this is a helper "
                    "liveness problem, not a permission one. While it is down every "
                    "desktop action it performs fails: OS-level screenshots, clicks, "
                    "keystrokes, window geometry. Check its launchd job "
                    "(`launchctl list | grep com.ava.permissions-helper`) and "
                    "$AVA_HOME/logs/permissions-helper.log."
                ),
            )
        if result["preflight_screen"]:
            return ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE)
        return ScreenCaptureStatus(
            state=ScreenCaptureState.NO_GRANT, diagnostic=_NO_GRANT_DIAGNOSTIC
        )


def check_accessibility(
    *, sock_path: str | Path | None = None, settle_s: float = _PROBE_SETTLE_S
) -> AccessibilityStatus:
    """Report whether the helper holds the Accessibility grant.

    The helper posts the synthetic input and reads the accessibility tree, so a
    caller-side probe would report a different process's grant. An unreachable
    helper is distinct from a missing grant: its answer was never read and the
    repair is its launchd job, not System Settings.
    """
    deadline = time.monotonic() + settle_s
    while True:
        try:
            result = ping(sock_path=sock_path)
        except PermissionsHelperError as exc:
            if time.monotonic() < deadline:
                time.sleep(_PROBE_RETRY_DELAY_S)
                continue
            return AccessibilityStatus(
                state=AccessibilityState.HELPER_UNREACHABLE,
                diagnostic=(
                    f"The permissions helper did not answer on its socket ({exc}), so its "
                    "Accessibility grant could not be read -- this is a helper liveness "
                    "problem, not a permission one. While it is down every desktop action "
                    "it performs fails: OS-level screenshots, clicks, keystrokes, window "
                    "geometry. Check its launchd job "
                    "(`launchctl list | grep com.ava.permissions-helper`) and "
                    "$AVA_HOME/logs/permissions-helper.log."
                ),
            )
        if result["ax_trusted"] is True:
            return AccessibilityStatus(state=AccessibilityState.GRANTED)
        return AccessibilityStatus(
            state=AccessibilityState.NOT_GRANTED, diagnostic=_NO_AX_GRANT_DIAGNOSTIC
        )
