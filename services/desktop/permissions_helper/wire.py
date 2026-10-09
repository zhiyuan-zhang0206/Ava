"""Typed result shapes of the signed permissions helper JSON protocol."""

from __future__ import annotations

from typing import NotRequired, TypedDict

# Per-method `result` shapes. Each mirrors the object the Swift daemon's matching
# handler returns (`services/desktop/permissions_helper/helper/main.swift`); they are the typed face
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
    ax_act_v1: NotRequired[bool]  # the helper serves `ax_act`
    ax_act_v2: NotRequired[bool]  # reported actions and exact text selection
    native_input_v1: NotRequired[bool]  # buttons/counts/modifiers/atomic holds


class ScreencaptureResult(TypedDict):
    path: str
    bytes: int  # PNG size on disk, or -1 if it could not be stat'd


class ClickPoint(TypedDict):
    x: float
    y: float


class ClickResult(TypedDict):
    clicked: ClickPoint
    double: bool
    button: NotRequired[str]
    click_count: NotRequired[int]


class MoveResult(TypedDict):
    moved: ClickPoint


class DragResult(TypedDict):
    start: ClickPoint
    end: ClickPoint


class TypeResult(TypedDict):
    typed: int  # characters sent


class KeyResult(TypedDict):
    key: int  # the virtual keycode pressed
    cmd: bool


class ScrollResult(TypedDict):
    scrolled: int  # dy pixels applied
    dx: NotRequired[int]


class AppInfo(TypedDict):
    pid: int
    name: str | None
    bundle_id: str | None


class AppsResult(TypedDict):
    apps: list[AppInfo]


class FocusAppResult(TypedDict):
    focused: bool
    pid: int


class WindowGeometry(TypedDict):
    x: float
    y: float
    w: float
    h: float


class AxWindowInfo(WindowGeometry):
    app: str


class WindowInfo(WindowGeometry):
    owner: str


class WindowRow(WindowGeometry):
    window_id: int
    pid: int
    owner: str | None
    title: str | None
    on_screen: bool


class WindowsResult(TypedDict):
    windows: list[WindowRow]


class WindowCaptureResult(ScreencaptureResult):
    origin: ClickPoint
    scale: float
    width: int
    height: int


class AxNode(TypedDict):
    """One raw accessibility element; geometry is logical points. Absent keys
    mean the app did not expose that attribute."""

    id: int  # raw id: valid for `ax_act` / `scope` until a later walk replaces the table
    fp: str  # path fingerprint: the same element keeps it across walks
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


class AxActResult(TypedDict):
    """Outcome of one `ax_act`. `stale` means the raw id is gone or its element
    changed (nothing was done); `unanswered` means the app did not answer in time
    (the action may still have run). Geometry is logical points."""

    completed: bool
    stale: NotRequired[bool]
    unanswered: NotRequired[bool]
    action: NotRequired[str]
    role: NotRequired[str]
    label: NotRequired[str]
    x: NotRequired[float]
    y: NotRequired[float]
    w: NotRequired[float]
    h: NotRequired[float]


class AxTreeResult(TypedDict):
    app: str
    pid: int
    windows: int
    framework: str  # "electron" / "cef" / "chromium" when the bundle ships one, else ""
    ax_enable: NotRequired[
        str
    ]  # Chromium switch: n/a | off | set | already | failed (older helpers omit it)
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
