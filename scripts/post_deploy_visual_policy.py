"""Pure policy for the post-deploy visual regression gate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

P2_EXIT_CODE = 10
P0_EXIT_CODE = 20
MAX_DIFF_PIXEL_RATIO = 0.001
CHANNEL_DELTA_THRESHOLD = 16
SURFACE_ROUTES = {
    "login": "/login",
    "home": "/",
    "fleet": "/fleet",
    "control": "/control",
    "run-timeline": "/insights/run/1",
}
SURFACE_PATH_PREFIXES: dict[str, tuple[str, ...]] = {
    "login-card": ("ui/web/src/app/login/", "ui/web/src/components/auth/"),
    "home-sidebar": (
        "ui/web/src/components/agent-sidebar",
        "ui/web/src/components/home-layout.tsx",
    ),
    "home-header": (
        "ui/web/src/components/header-bar.tsx",
        "ui/web/src/components/home-layout.tsx",
        "ui/web/src/app/page.tsx",
    ),
    "home-composer": ("ui/web/src/components/composer.tsx", "ui/web/src/app/page.tsx"),
    "control-header": ("ui/web/src/app/control/",),
    "control-nav": ("ui/web/src/app/control/",),
    "fleet-main": ("ui/web/src/app/fleet/", "ui/web/src/components/fleet/"),
    "run-timeline-main": (
        "ui/web/src/app/insights/run/",
        "ui/web/src/components/run-timeline/",
    ),
}
VIEWPORTS = {"desktop": (1280, 800), "narrow": (390, 844)}
THEMES = ("light", "dark")
_WAVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
STRUCTURAL_SPECS = {
    "login": {
        "ready": "form",
        "visible": ("form",),
        "controls": ("input[type='password']",),
        "nonempty": ("form",),
    },
    "home": {
        "ready": "[data-testid='composer']",
        "visible": (
            "#main-content",
            "[data-testid='timeline-surface']",
            "[data-testid='composer']",
        ),
        "controls": ("[data-testid='composer-input']",),
        "nonempty": ("[data-testid='timeline-surface']", "[data-testid='composer']"),
    },
    "fleet": {
        "ready": "[aria-label='Fleet relationship graph']",
        "visible": ("#main-content", "[aria-label='Fleet relationship graph']"),
        "controls": ("a[href='/']",),
        "nonempty": ("#main-content",),
    },
    "control": {
        "ready": "[aria-label='Control sections']",
        "visible": ("header", "[aria-label='Control sections']"),
        "controls": ("a[href='/']",),
        "nonempty": ("[aria-label='Control sections']",),
    },
    "run-timeline": {
        "ready": "[data-testid='run-timeline-geometry']",
        "visible": ("#main-content", "[aria-label='Run timeline chart']"),
        "controls": ("button[type='submit']",),
        "nonempty": ("[aria-label='Run timeline chart']",),
    },
}


@dataclass(frozen=True)
class DiffRegion:
    """A rectangular changed-pixel region."""

    x: int
    y: int
    width: int
    height: int
    pixels: int


@dataclass(frozen=True)
class StableDiff:
    """Diff regions observed in both captures."""

    stable_regions: tuple[DiffRegion, ...]
    changed_pixels: int
    ratio: float
    drifted: bool


@dataclass(frozen=True)
class Attribution:
    """Whether wave changes explain one screenshot surface."""

    expected: bool
    matched_paths: tuple[str, ...]


@dataclass(frozen=True)
class EscalationDecision:
    """Notification severity and persisted deployment-wave counters."""

    severity: Literal["P0", "P2"] | None
    next_counts: dict[str, int]


def validate_wave_id(value: object) -> str:
    """Keep artifact paths beneath their configured output root."""
    if not isinstance(value, str) or not _WAVE_ID.fullmatch(value):
        raise ValueError(
            "wave SHA may contain only letters, digits, dots, underscores, and hyphens"
        )
    return value


def route_for_surface(surface: str) -> str:
    """Resolve a route for a page or named crop surface."""
    if surface in SURFACE_ROUTES:
        return SURFACE_ROUTES[surface]
    page = surface.split("-", 1)[0]
    if surface.startswith("run-timeline-"):
        page = "run-timeline"
    return SURFACE_ROUTES[page]


def attribute_surface(surface: str, changed_paths: list[str]) -> Attribution:
    """Classify drift by intersecting its static surface map with wave paths."""
    prefixes = SURFACE_PATH_PREFIXES.get(surface)
    if prefixes is None:
        return Attribution(expected=False, matched_paths=())
    matches = tuple(sorted(path for path in changed_paths if path.startswith(prefixes)))
    return Attribution(bool(matches), matches)


def _intersection(first: DiffRegion, second: DiffRegion) -> DiffRegion | None:
    left = max(first.x, second.x)
    top = max(first.y, second.y)
    right = min(first.x + first.width, second.x + second.width)
    bottom = min(first.y + first.height, second.y + second.height)
    if left >= right or top >= bottom:
        return None
    area = (right - left) * (bottom - top)
    return DiffRegion(left, top, right - left, bottom - top, min(first.pixels, second.pixels, area))


def classify_stable_diff(
    first_regions: list[DiffRegion],
    second_regions: list[DiffRegion],
    *,
    total_pixels: int,
) -> StableDiff:
    """Keep only region intersections repeated in both captures."""
    if total_pixels <= 0:
        raise ValueError("total_pixels must be positive")
    stable = tuple(
        overlap
        for first in first_regions
        for second in second_regions
        if (overlap := _intersection(first, second)) is not None
    )
    changed_pixels = sum(region.pixels for region in stable)
    ratio = changed_pixels / total_pixels
    return StableDiff(stable, changed_pixels, ratio, ratio > MAX_DIFF_PIXEL_RATIO)


def unexpected_pixel_surfaces(diffs: list[dict[str, object]]) -> tuple[str, ...]:
    """Drift surfaces feeding escalation, in stable order.

    Only classifier-marked ``unexpected`` crops escalate. ``baseline-missing``
    crops are a setup state (the golden does not exist yet) and never feed the
    P2/P0 decision; they stay visible in ``probes.json`` for the accept flow.
    """
    return tuple(
        sorted(
            {str(diff["crop_surface"]) for diff in diffs if diff["classification"] == "unexpected"}
        )
    )


def decide_escalation(
    structural_failure_count: int,
    unexpected_surfaces: tuple[str, ...],
    previous_counts: dict[str, int],
    *,
    deployment_wave: bool,
) -> EscalationDecision:
    """Apply immediate structural P0 and deployment-only two-wave promotion."""
    if deployment_wave:
        next_counts = {
            surface: previous_counts.get(surface, 0) + 1 for surface in unexpected_surfaces
        }
    else:
        next_counts = dict(previous_counts)
    repeated = deployment_wave and any(
        next_counts.get(surface, 0) >= 2 for surface in unexpected_surfaces
    )
    if structural_failure_count or repeated:
        severity: Literal["P0", "P2"] | None = "P0"
    elif unexpected_surfaces:
        severity = "P2"
    else:
        severity = None
    return EscalationDecision(severity, next_counts)


def extract_gateway_started_at(payload: dict[str, object]) -> float:
    """Validate and return the gateway process birth from its health API."""
    if payload["name"] != "gateway":
        raise ValueError("health response did not identify a gateway")
    started_at = payload["started_at"]
    if not isinstance(started_at, int | float):
        raise TypeError("gateway started_at must be numeric")
    return float(started_at)


def extract_gateway_sha(payload: dict[str, object]) -> str:
    """Validate and return the commit frozen by the serving gateway."""
    sha = payload["sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise ValueError("gateway health sha must be a hexadecimal Git commit")
    return sha
