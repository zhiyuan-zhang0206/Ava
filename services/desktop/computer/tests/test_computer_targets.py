"""Observation selectors reject malformed identities before a native request."""

from __future__ import annotations

from typing import Any

import pytest

from services.desktop.computer.errors import ComputerUseError
from services.desktop.computer.targets import (
    AppTarget,
    CaptureFrame,
    CaptureRegion,
    CoordinateSpace,
    WindowTarget,
    app_selector,
)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"x": 0, "y": 0, "w": 0, "h": 20},
        {"x": False, "y": 0, "w": 20, "h": 20},
        {"x": 0.5, "y": 0, "w": 20, "h": 20},
        {"x": 0, "y": 0, "w": 20, "h": 20, "screen": 1},
    ],
)
def test_region_rejects_invalid_raw_inputs(value: Any) -> None:
    with pytest.raises(ComputerUseError):
        CaptureRegion.parse(value)


def test_region_preserves_negative_global_origin() -> None:
    assert CaptureRegion.parse({"x": -400, "y": -20, "w": 200, "h": 100}) == CaptureRegion(
        -400, -20, 200, 100
    )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"pid": 123},
        {"pid": 123, "window_id": True},
        {"pid": "123", "window_id": 9},
        {"pid": 0, "window_id": 9},
        {"pid": 2**31, "window_id": 9},
        {"pid": 123, "window_id": 2**32},
        {"pid": 123, "window_id": 9, "app": "Mail"},
    ],
)
def test_window_identity_rejects_partial_or_invalid_selectors(value: Any) -> None:
    with pytest.raises(ComputerUseError):
        WindowTarget.parse(value)


def test_window_identity_preserves_both_identifiers() -> None:
    assert WindowTarget.parse({"pid": 123, "window_id": 9}) == WindowTarget(123, 9)


@pytest.mark.parametrize("value", ["", "  ", False, 123])
def test_explicit_app_selector_never_defaults_invalid_input(value: Any) -> None:
    with pytest.raises(ComputerUseError):
        app_selector(value)


def test_optional_app_selector_is_exact() -> None:
    assert app_selector(None) is None
    assert app_selector("com.apple.mail") == "com.apple.mail"


def test_region_frame_can_be_passed_back_without_manual_coordinate_conversion() -> None:
    frame = CaptureFrame(CoordinateSpace.REGION_PIXELS, -300, 20, 2, 400, 200)
    returned = frame.as_dict()
    assert CaptureFrame.parse(returned).global_point(100, 50) == (-250, 45)


@pytest.mark.parametrize("point", [(-1, 50), (400, 50), (50, 200), (True, 50), (float("nan"), 50)])
def test_region_frame_refuses_outside_or_invalid_pointer_coordinates(
    point: tuple[Any, Any],
) -> None:
    frame = CaptureFrame(CoordinateSpace.REGION_PIXELS, 10, 20, 2, 400, 200)
    with pytest.raises(ComputerUseError):
        frame.global_point(*point)


def test_window_frame_never_falls_back_to_global_hid_input() -> None:
    frame = CaptureFrame(CoordinateSpace.WINDOW_PIXELS, 10, 20, 2, 400, 200, WindowTarget(123, 9))
    assert CaptureFrame.parse(frame.as_dict()) == frame
    with pytest.raises(ComputerUseError, match="unsupported"):
        frame.global_point(100, 50)


@pytest.mark.parametrize(
    "changes",
    [
        {"coordinate_space": "screen"},
        {"scale": 0},
        {"scale": float("inf")},
        {"scale": True},
        {"pixels": {"width": 0, "height": 200}},
        {"origin": {"x": 0}},
        {"target": {"pid": 123, "window_id": 9}},
    ],
)
def test_malformed_frame_never_changes_the_coordinate_contract(changes: dict[str, Any]) -> None:
    frame = CaptureFrame(CoordinateSpace.REGION_PIXELS, 0, 0, 2, 400, 200).as_dict()
    with pytest.raises(ComputerUseError):
        CaptureFrame.parse({**frame, **changes})


def test_window_frame_requires_both_native_identity_fields() -> None:
    frame = CaptureFrame(CoordinateSpace.REGION_PIXELS, 0, 0, 2, 400, 200).as_dict()
    with pytest.raises(ComputerUseError, match="require a target"):
        CaptureFrame.parse({**frame, "coordinate_space": CoordinateSpace.WINDOW_PIXELS.value})


def test_frame_refuses_numeric_overflow_before_native_input() -> None:
    frame = CaptureFrame(CoordinateSpace.REGION_PIXELS, 0, 0, 2, 400, 200).as_dict()
    with pytest.raises(ComputerUseError, match="finite"):
        CaptureFrame.parse({**frame, "origin": {"x": 10**1000, "y": 0}})
    tiny_scale = CaptureFrame(CoordinateSpace.REGION_PIXELS, 0, 0, 5e-324, 400, 200)
    with pytest.raises(ComputerUseError, match="mapped x"):
        tiny_scale.global_point(1, 1)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"app": "Mail"},
        {"pid": 123, "bundle_id": "com.apple.mail"},
        {"pid": False},
        {"pid": "123"},
        {"pid": 2**31},
        {"bundle_id": " "},
    ],
)
def test_foreground_app_target_requires_one_exact_native_selector(value: Any) -> None:
    with pytest.raises(ComputerUseError):
        AppTarget.parse(value)


def test_foreground_app_target_preserves_pid_or_bundle_id() -> None:
    assert AppTarget.parse({"pid": 123}) == AppTarget(pid=123)
    assert AppTarget.parse({"bundle_id": "com.apple.mail"}) == AppTarget(bundle_id="com.apple.mail")
