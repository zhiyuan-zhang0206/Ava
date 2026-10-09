"""Explicit observation selectors and logical screen regions.

Selectors are carried by each call. This module keeps no selected-app state;
native callers resolve identities against the live window server each time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any, NotRequired, TypedDict, cast

from .errors import ComputerUseError


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ComputerUseError(f"{name} must be an integer")
    if positive and value <= 0:
        raise ComputerUseError(f"{name} must be positive")
    return value


def _object(value: Any, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(cast(dict[Any, Any], value)) != keys:
        raise ComputerUseError(f"{name} must contain exactly {', '.join(sorted(keys))}")
    return cast(dict[str, Any], value)


@dataclass(frozen=True)
class CaptureRegion:
    """A rectangle in global logical points, with an upper-left origin."""

    x: int
    y: int
    w: int
    h: int

    @classmethod
    def parse(cls, value: Any) -> CaptureRegion:
        value = _object(value, "region", {"x", "y", "w", "h"})
        return cls(
            _integer(value["x"], "region.x"),
            _integer(value["y"], "region.y"),
            _integer(value["w"], "region.w", positive=True),
            _integer(value["h"], "region.h", positive=True),
        )


@dataclass(frozen=True)
class WindowTarget:
    """A window-server ID paired with its owning process identity."""

    pid: int
    window_id: int

    @classmethod
    def parse(cls, value: Any) -> WindowTarget:
        value = _object(value, "target", {"pid", "window_id"})
        pid = _integer(value["pid"], "target.pid", positive=True)
        window_id = _integer(value["window_id"], "target.window_id", positive=True)
        if pid > 2**31 - 1 or window_id > 2**32 - 1:
            raise ComputerUseError("target identifiers exceed the native range")
        return cls(pid, window_id)


@dataclass(frozen=True)
class AppTarget:
    """One running app selected for an explicit foreground activation."""

    pid: int | None = None
    bundle_id: str | None = None

    @classmethod
    def parse(cls, value: Any) -> AppTarget:
        if not isinstance(value, dict):
            raise ComputerUseError("app target must be an object")
        value = cast(dict[str, Any], value)
        if set(value) == {"pid"}:
            pid = _integer(value["pid"], "app target.pid", positive=True)
            if pid > 2**31 - 1:
                raise ComputerUseError("app target.pid exceeds the native range")
            return cls(pid=pid)
        if set(value) == {"bundle_id"}:
            bundle_id = value["bundle_id"]
            if not isinstance(bundle_id, str) or not bundle_id.strip():
                raise ComputerUseError("app target.bundle_id must be a nonempty string")
            return cls(bundle_id=bundle_id)
        raise ComputerUseError("app target must contain exactly pid or bundle_id")


def app_selector(value: Any) -> str | None:
    """Validate an optional exact display-name or bundle-ID selector."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ComputerUseError("app must be a nonempty display name or bundle identifier")
    return value


class CoordinateSpace(StrEnum):
    REGION_PIXELS = "region_pixels"
    WINDOW_PIXELS = "window_pixels"


class FrameOrigin(TypedDict):
    x: float
    y: float


class FramePixels(TypedDict):
    width: int
    height: int


class TargetIdentity(TypedDict):
    pid: int
    window_id: int


class ObservationFrame(TypedDict):
    """A stateless screenshot-local pixel frame returned to the caller."""

    coordinate_space: str
    origin: FrameOrigin
    scale: float
    pixels: FramePixels
    target: NotRequired[TargetIdentity]


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComputerUseError(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise ComputerUseError(f"{name} must be a finite number") from None
    if not isfinite(number):
        raise ComputerUseError(f"{name} must be a finite number")
    return number


@dataclass(frozen=True)
class CaptureFrame:
    """Explicit pixel origin and scale; window identity does not grant input support."""

    coordinate_space: CoordinateSpace
    x: float
    y: float
    scale: float
    width: int
    height: int
    target: WindowTarget | None = None

    @classmethod
    def parse(cls, value: Any) -> CaptureFrame:
        required = {"coordinate_space", "origin", "scale", "pixels"}
        if not isinstance(value, dict):
            raise ComputerUseError("frame must be an object")
        value = cast(dict[str, Any], value)
        if not required <= set(value) <= required | {"target"}:
            raise ComputerUseError("frame needs coordinate_space, origin, scale and pixels")
        try:
            space = CoordinateSpace(value["coordinate_space"])
        except (ValueError, TypeError):
            raise ComputerUseError("unknown frame coordinate_space") from None
        origin = _object(value["origin"], "frame.origin", {"x", "y"})
        pixels = _object(value["pixels"], "frame.pixels", {"width", "height"})
        scale = _number(value["scale"], "frame.scale")
        if scale <= 0:
            raise ComputerUseError("frame.scale must be positive")
        target = WindowTarget.parse(value["target"]) if "target" in value else None
        if (space == CoordinateSpace.WINDOW_PIXELS) != (target is not None):
            raise ComputerUseError("only window_pixels frames require a target")
        return cls(
            space,
            _number(origin["x"], "frame.origin.x"),
            _number(origin["y"], "frame.origin.y"),
            scale,
            _integer(pixels["width"], "frame.pixels.width", positive=True),
            _integer(pixels["height"], "frame.pixels.height", positive=True),
            target,
        )

    def as_dict(self) -> ObservationFrame:
        frame: ObservationFrame = {
            "coordinate_space": self.coordinate_space.value,
            "origin": {"x": self.x, "y": self.y},
            "scale": self.scale,
            "pixels": {"width": self.width, "height": self.height},
        }
        if self.target is not None:
            frame["target"] = {"pid": self.target.pid, "window_id": self.target.window_id}
        return frame

    def global_point(self, x: Any, y: Any) -> tuple[float, float]:
        """Map a region-local pixel to shared-desktop logical points.

        A window screenshot is not a global HID input target. Refuse that
        mapping until a supported target-scoped input path owns the operation.
        """
        if self.coordinate_space == CoordinateSpace.WINDOW_PIXELS:
            raise ComputerUseError("window frame pointer input is unsupported")
        px, py = _number(x, "x"), _number(y, "y")
        if not (0 <= px < self.width and 0 <= py < self.height):
            raise ComputerUseError("point is outside the observation frame")
        return (
            _number(self.x + px / self.scale, "mapped x"),
            _number(self.y + py / self.scale, "mapped y"),
        )
