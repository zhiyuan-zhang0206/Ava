from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

HELPER_HOLDER = "com.meiva.permissions-helper"


class RequestMode(StrEnum):
    PROMPT = "prompt"

    SYSTEM_SETTINGS = "system_settings"

    LIVE_EVENT = "live_event"

    REPORT_ONLY = "report_only"


class Grant(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    NOT_DETERMINED = "not_determined"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PermissionItem:
    id: str
    group: str
    title: str
    why: str
    mode: RequestMode
    pane: str
    verify: str
    services: tuple[str, ...] = ()
    required: bool = True
    helper_restart: bool = False
    ping_key: str = ""
    home_subpath: str = ""


SYSTEM_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?{pane}"


def settings_url(item: PermissionItem) -> str:
    return SYSTEM_SETTINGS_URL.format(pane=item.pane)


FOLDER_ITEMS: tuple[PermissionItem, ...] = (
    PermissionItem(
        id="desktop-folder",
        group="files",
        title="Desktop folder",
        why="Read and write files on your Desktop when Meiva opens or saves one for you.",
        mode=RequestMode.PROMPT,
        pane="Privacy_FilesAndFolders",
        verify="Finder shows Meiva's folder row as enabled in Files and Folders.",
        services=("kTCCServiceSystemPolicyDesktopFolder",),
        home_subpath="Desktop",
    ),
    PermissionItem(
        id="documents-folder",
        group="files",
        title="Documents folder",
        why="Read and write files in your Documents folder on request.",
        mode=RequestMode.PROMPT,
        pane="Privacy_FilesAndFolders",
        verify="Finder shows Meiva's folder row as enabled in Files and Folders.",
        services=("kTCCServiceSystemPolicyDocumentsFolder",),
        home_subpath="Documents",
    ),
    PermissionItem(
        id="downloads-folder",
        group="files",
        title="Downloads folder",
        why="Pick up and hand over files in your Downloads folder.",
        mode=RequestMode.PROMPT,
        pane="Privacy_FilesAndFolders",
        verify="Finder shows Meiva's folder row as enabled in Files and Folders.",
        services=("kTCCServiceSystemPolicyDownloadsFolder",),
        home_subpath="Downloads",
    ),
)

AUTOMATION_TARGETS: tuple[tuple[str, str], ...] = (
    ("finder", "Finder"),
    ("terminal", "Terminal"),
    ("system-events", "System Events"),
    ("safari", "Safari"),
    ("google-chrome", "Google Chrome"),
)

AUTOMATION_ITEMS: tuple[PermissionItem, ...] = tuple(
    PermissionItem(
        id=f"automation-{suffix}",
        group="automation",
        title=f"Control {app}",
        why=f"Ask {app} to open, focus or inspect a window while it drives your desktop.",
        mode=RequestMode.LIVE_EVENT,
        pane="Privacy_Automation",
        verify=f"Privacy & Security > Automation lists Meiva with {app} ticked.",
        services=("kTCCServiceAppleEvents",),
    )
    for suffix, app in AUTOMATION_TARGETS
)

SCREEN_ITEM = PermissionItem(
    id="screen-recording",
    group="desktop",
    title="Screen Recording",
    why="Take screenshots and window captures -- every look at your screen goes through it.",
    mode=RequestMode.PROMPT,
    pane="Privacy_ScreenCapture",
    verify="Meiva is listed and ticked in Screen & System Audio Recording.",
    services=("kTCCServiceScreenCapture",),
    helper_restart=True,
    ping_key="preflight_screen",
)

ACCESSIBILITY_ITEM = PermissionItem(
    id="accessibility",
    group="desktop",
    title="Accessibility",
    why="Click, type, scroll and read the window contents on your behalf.",
    mode=RequestMode.PROMPT,
    pane="Privacy_Accessibility",
    verify="Meiva is listed and ticked in Accessibility.",
    services=("kTCCServiceAccessibility",),
    helper_restart=True,
    ping_key="ax_trusted",
)

DESKTOP_ITEMS: tuple[PermissionItem, ...] = (SCREEN_ITEM, ACCESSIBILITY_ITEM)

REPORT_ONLY_ITEMS: tuple[PermissionItem, ...] = (
    PermissionItem(
        id="app-data",
        group="extended",
        title="App data",
        why="Not used by any shipped feature today; reported so a future one is not a surprise.",
        mode=RequestMode.REPORT_ONLY,
        pane="Privacy_FilesAndFolders",
        verify="Privacy & Security > Files and Folders lists Meiva for the app's container.",
        services=("kTCCServiceSystemPolicyAppData",),
        required=False,
    ),
    PermissionItem(
        id="media-library",
        group="extended",
        title="Media & Apple Music",
        why="Not used by any shipped feature today; reported for completeness.",
        mode=RequestMode.REPORT_ONLY,
        pane="Privacy_Media",
        verify="Privacy & Security > Media & Apple Music lists Meiva.",
        services=("kTCCServiceMediaLibrary", "kTCCServicePhotos"),
        required=False,
    ),
    PermissionItem(
        id="icloud-drive",
        group="extended",
        title="iCloud Drive",
        why="Not used by any shipped feature today; reported for completeness.",
        mode=RequestMode.REPORT_ONLY,
        pane="Privacy_FilesAndFolders",
        verify="Privacy & Security > Files and Folders lists the iCloud Drive surface.",
        services=("kTCCServiceFileProviderDomain", "kTCCServiceUbiquity"),
        required=False,
    ),
    PermissionItem(
        id="full-disk-access",
        group="extended",
        title="Full Disk Access",
        why="Not requested by Meiva: the helper's file APIs stay inside the folders above.",
        mode=RequestMode.REPORT_ONLY,
        pane="Privacy_AllFiles",
        verify="Privacy & Security > Full Disk Access lists Meiva.",
        services=("kTCCServiceSystemPolicyAllFiles",),
        required=False,
    ),
    PermissionItem(
        id="developer-tool",
        group="extended",
        title="Developer Tools",
        why="Not requested by Meiva; reported only so the row is not mistaken for a fault.",
        mode=RequestMode.REPORT_ONLY,
        pane="Privacy_DeveloperTools",
        verify="Privacy & Security > Developer Tools lists Meiva.",
        services=("kTCCServiceDeveloperTool",),
        required=False,
    ),
)

CATALOG: tuple[PermissionItem, ...] = (
    *DESKTOP_ITEMS,
    *FOLDER_ITEMS,
    *AUTOMATION_ITEMS,
    *REPORT_ONLY_ITEMS,
)

REQUIRED_ITEMS: tuple[PermissionItem, ...] = tuple(item for item in CATALOG if item.required)

PREFLIGHT_SERVICES: tuple[str, ...] = tuple(
    dict.fromkeys(service for item in CATALOG for service in item.services)
)

PING_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(item.ping_key for item in CATALOG if item.ping_key)
)

ITEMS_BY_ID: dict[str, PermissionItem] = {item.id: item for item in CATALOG}
