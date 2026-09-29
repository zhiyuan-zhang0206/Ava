"""Small stdlib-only shape checks for durable JSON documents.

Callers supply their own error type and wording so existing document contracts
keep their first error while sharing the mechanical field checks.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import cast


def object_fields(value: object, *, error_type: type[Exception], message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise error_type(message)
    return cast(dict[str, object], value)


def exact_fields(
    raw: Mapping[str, object],
    expected: Collection[str],
    *,
    error_type: type[Exception],
    message: str,
) -> None:
    if set(raw) != set(expected):
        raise error_type(message)


def strict_int(
    raw: Mapping[str, object],
    name: str,
    *,
    error_type: type[Exception],
    message_template: str,
) -> int:
    value = raw[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise error_type(message_template.format(name=name))
    return value


def strict_string(
    raw: Mapping[str, object],
    name: str,
    *,
    error_type: type[Exception],
    message_template: str,
) -> str:
    value = raw[name]
    if not isinstance(value, str):
        raise error_type(message_template.format(name=name))
    return value


def optional_string(
    raw: Mapping[str, object],
    name: str,
    *,
    error_type: type[Exception],
    message_template: str,
) -> str | None:
    value = raw[name]
    if value is not None and not isinstance(value, str):
        raise error_type(message_template.format(name=name))
    return value


def string_map(
    raw: Mapping[str, object],
    name: str,
    *,
    object_error_type: type[Exception],
    object_message_template: str,
    pairs_error_type: type[Exception],
    pairs_message_template: str,
) -> dict[str, str] | None:
    value = raw[name]
    if value is None:
        return None
    if not isinstance(value, dict):
        raise object_error_type(object_message_template.format(name=name))
    items = cast(dict[object, object], value)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in items.items()):
        raise pairs_error_type(pairs_message_template.format(name=name))
    return cast(dict[str, str], value)
