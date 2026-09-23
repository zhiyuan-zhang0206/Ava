"""Temporary checkpoint-postgres 3.1.2 fix for historical delta walk pagination.

Remove this wrapper after langgraph#8448 / #8556 ships in a stable release:
run the historical-walk regression, remove this module, then upgrade the pin.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import wraps
from importlib.metadata import version
from inspect import Parameter, signature
from typing import Any, cast

from langgraph.checkpoint.postgres.base import BasePostgresSaver

_EXPECTED_PARAMETERS = (
    "target_id",
    "channels",
    "parent_of",
    "ver_by_i_by_cid",
    "hb_by_i_by_cid",
    "inline_by_i_by_cid",
    "chain_by_ch",
    "seed_ver_by_ch",
    "seed_inline_by_ch",
    "walk_cursor_by_ch",
    "seeded",
)
_installed_descriptor: list[object] = []


def install_checkpoint_postgres_walk_patch() -> None:
    """Keep an unseen target out of the walk cursor until the next SQL page."""
    dependency_version = version("langgraph-checkpoint-postgres")
    if dependency_version != "3.1.2":
        raise RuntimeError(
            "checkpoint-postgres walk patch requires langgraph-checkpoint-postgres==3.1.2; "
            f"found {dependency_version}"
        )

    descriptor = vars(BasePostgresSaver)["_try_advance_walks"]
    if _installed_descriptor and descriptor is _installed_descriptor[0]:
        return
    if _installed_descriptor or not isinstance(descriptor, staticmethod):
        raise RuntimeError("checkpoint-postgres _try_advance_walks was replaced outside Ava")

    original = cast(Callable[..., None], descriptor.__func__)
    method_signature = signature(original)
    if (
        original.__module__ != "langgraph.checkpoint.postgres.base"
        or original.__qualname__ != "BasePostgresSaver._try_advance_walks"
        or hasattr(original, "__wrapped__")
        or tuple(method_signature.parameters) != _EXPECTED_PARAMETERS
        or any(
            parameter.kind is not Parameter.POSITIONAL_OR_KEYWORD
            or parameter.default is not Parameter.empty
            for parameter in method_signature.parameters.values()
        )
        or method_signature.return_annotation not in (None, "None")
    ):
        raise RuntimeError("checkpoint-postgres _try_advance_walks signature or identity changed")

    @wraps(original)
    def advance_walks(
        target_id: str,
        channels: Sequence[str],
        parent_of: Mapping[str, str | None],
        ver_by_i_by_cid: Sequence[Mapping[str, str | None]],
        hb_by_i_by_cid: Sequence[Mapping[str, bool]],
        inline_by_i_by_cid: Sequence[Mapping[str, Any]],
        chain_by_ch: dict[str, list[str]],
        seed_ver_by_ch: dict[str, str | None],
        seed_inline_by_ch: dict[str, Any],
        walk_cursor_by_ch: dict[str, str | None],
        seeded: set[str],
    ) -> None:
        if target_id not in parent_of:
            return
        original(
            target_id,
            channels,
            parent_of,
            ver_by_i_by_cid,
            hb_by_i_by_cid,
            inline_by_i_by_cid,
            chain_by_ch,
            seed_ver_by_ch,
            seed_inline_by_ch,
            walk_cursor_by_ch,
            seeded,
        )

    _installed_descriptor.append(staticmethod(advance_walks))
    BasePostgresSaver._try_advance_walks = _installed_descriptor[0]  # type: ignore[assignment]
