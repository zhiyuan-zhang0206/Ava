"""Strict input contracts and their derived MCP schemas."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import ComputerUseError
from .targets import ObservationFrame

Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Modifier = Literal["shift", "ctrl", "alt", "cmd"]
MouseButton = Literal["left", "right", "middle"]
WheelDelta = Annotated[int, Field(strict=True, ge=-(2**31), le=2**31 - 1)]


class ModifiedInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    modifiers: list[Modifier] = Field(default_factory=list[Modifier], max_length=4)

    @model_validator(mode="after")
    def unique_modifiers(self) -> ModifiedInput:
        if len(set(self.modifiers)) != len(self.modifiers):
            raise ValueError("modifiers must not contain duplicates")
        return self


class PositionedInput(ModifiedInput):
    frame: ObservationFrame | None = None


class ClickInput(PositionedInput):
    x: Coordinate
    y: Coordinate
    button: MouseButton = "left"
    click_count: int = Field(default=1, ge=1, le=3)
    double: bool = False
    duration_ms: Coordinate = Field(default=0, ge=0, le=5000)

    @model_validator(mode="after")
    def double_count_agree(self) -> ClickInput:
        if self.double and "click_count" in self.model_fields_set and self.click_count != 2:
            raise ValueError("double=true requires click_count=2 when both are supplied")
        return self


class KeyInput(ModifiedInput):
    key: str | None = Field(default=None, min_length=1)
    keycode: int | None = Field(default=None, ge=0, le=65535)
    cmd: bool = False
    duration_ms: Coordinate = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def one_key(self) -> KeyInput:
        if (self.key is None) == (self.keycode is None):
            raise ValueError("key requires exactly one key name or integer keycode")
        return self


class ScrollInput(PositionedInput):
    x: Coordinate | None = None
    y: Coordinate | None = None
    dx: WheelDelta = 0
    dy: WheelDelta = 0

    @model_validator(mode="after")
    def paired_position_and_delta(self) -> ScrollInput:
        if (self.x is None) != (self.y is None):
            raise ValueError("scroll requires both x and y when positioning explicitly")
        if self.frame is not None and self.x is None:
            raise ValueError("scroll frame requires explicit x and y")
        if not self.model_fields_set.intersection({"dx", "dy"}):
            raise ValueError("scroll requires dx or dy")
        return self


class MoveInput(PositionedInput):
    x: Coordinate
    y: Coordinate


class DragInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    frame: ObservationFrame | None = None
    start_x: Coordinate
    start_y: Coordinate
    end_x: Coordinate
    end_y: Coordinate


def validate_input[InputModel: BaseModel](
    model: type[InputModel], args: dict[str, Any]
) -> InputModel:
    """Validate action parameters while preserving the daemon's coordination envelope."""
    action = {key: value for key, value in args.items() if key not in {"task_id", "priority"}}
    try:
        return model.model_validate(action)
    except ValidationError as exc:
        raise ComputerUseError(str(exc)) from exc


def tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Expose the same input contract to MCP, plus screen coordination arguments."""
    schema = model.model_json_schema()
    schema["properties"].update(
        {
            "task_id": {"type": "integer"},
            "priority": {"type": "string", "enum": ["normal", "high"], "default": "normal"},
        }
    )
    return schema
