"""SDK and RPC share one cleanup timing owner and preserve the wire vocabulary."""

from typing import get_type_hints

import pytest
from pydantic import ValidationError

from ava.agents import ShellSessionsKill as SdkShellSessionsKill
from base.agents import ShellSessionKillTiming
from ops.rpc_schemas.terminate import ShellSessionsKill as RpcShellSessionsKill


@pytest.mark.parametrize("timing", list(ShellSessionKillTiming))
def test_sdk_rpc_share_timing_and_serialize_existing_values(timing: ShellSessionKillTiming) -> None:
    assert get_type_hints(SdkShellSessionsKill)["when"] is ShellSessionKillTiming
    assert RpcShellSessionsKill.model_fields["when"].annotation is ShellSessionKillTiming
    wire = {"when": timing.value, "killed": [2]}
    rpc = RpcShellSessionsKill.model_validate(wire)
    assert rpc.when is timing
    assert rpc.model_dump(mode="json") == wire
    sdk = SdkShellSessionsKill(when=timing, killed=[2])
    assert sdk.when is timing
    schema = RpcShellSessionsKill.model_json_schema()
    ref = schema["properties"]["when"]["$ref"]
    assert schema["$defs"][ref.rsplit("/", 1)[1]]["enum"] == ["now", "at_exit"]


@pytest.mark.parametrize("when", ["later", "", None])
def test_unknown_raw_timing_is_rejected_by_both_boundaries(when: object) -> None:
    with pytest.raises(ValidationError):
        RpcShellSessionsKill.model_validate({"when": when, "killed": []})
    with pytest.raises(ValueError, match="ShellSessionKillTiming"):
        SdkShellSessionsKill(when=when, killed=[])  # pyright: ignore[reportArgumentType]
