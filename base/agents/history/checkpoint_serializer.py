"""The checkpoint serializer: the framework allowlist plus a loud refusal of pre-split envelopes.

Checkpoints written before the channel models moved to ``agent.state_channels`` carry
``("agent.state", "<Name>")`` ext envelopes. Those pairs are no longer allowlisted, and the
stock serializer would hand back the raw dict for each, silently breaking the channel.
``CheckpointSerializer`` raises on load instead, so the thread that holds one fails visibly.
"""

from __future__ import annotations

from typing import Any

import ormsgpack
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

_PRE_SPLIT_MODULE = "agent.state"
# msgpack fixstr header (length 11) + the module name: the prefix of a pre-split envelope tuple.
_PRE_SPLIT_PREFIX = b"\xab" + _PRE_SPLIT_MODULE.encode()


class PreSplitCheckpointEnvelopeError(ValueError):
    """A checkpoint blob names a pre-split ``agent.state`` type."""


def _names_pre_split_type(blob: bytes) -> bool:
    found = False

    def probe(_code: int, data: bytes) -> None:
        nonlocal found
        try:
            payload: Any = ormsgpack.unpackb(
                data, ext_hook=probe, option=ormsgpack.OPT_NON_STR_KEYS
            )
        except ormsgpack.MsgpackDecodeError:
            return
        if isinstance(payload, tuple | list) and payload and payload[0] == _PRE_SPLIT_MODULE:  # pyright: ignore[reportUnknownArgumentType]
            found = True
        return

    try:
        ormsgpack.unpackb(blob, ext_hook=probe, option=ormsgpack.OPT_NON_STR_KEYS)
    except ormsgpack.MsgpackDecodeError:
        return False
    return found


class CheckpointSerializer(JsonPlusSerializer):
    """`JsonPlusSerializer` that refuses a blob holding a pre-split ``agent.state`` envelope."""

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        tag, blob = data
        if tag == "msgpack" and _PRE_SPLIT_PREFIX in blob and _names_pre_split_type(blob):
            raise PreSplitCheckpointEnvelopeError(
                "checkpoint holds a pre-split legacy ('agent.state', <Name>) envelope, which is "
                "no longer readable; delete the checkpoint rows (checkpoints, checkpoint_blobs, "
                "checkpoint_writes) of the thread that holds it"
            )
        return super().loads_typed(data)
