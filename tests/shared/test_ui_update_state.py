# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""Home lifecycle mutexes: bounded waits that name the last holder."""

from __future__ import annotations

import json

import pytest

from shared import ui_update_state as state
from shared.platform import LockTimeoutError


def test_resource_holder_does_not_block_short_owner_publication(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state.shared.paths, "ava_home", lambda: tmp_path)

    with state.resource_lock(purpose="ops.pause"):
        resource = json.loads(state._holder_path(state.lifecycle_lock_path()).read_text())
        assert resource["state"] == "held"
        assert resource["purpose"] == "ops.pause"
        with state.lifecycle_lock():
            owner = json.loads(state._holder_path(state.owner_lock_path()).read_text())
            assert owner["state"] == "held"
            assert "test_resource_holder" in owner["purpose"]

        with (
            pytest.raises(LockTimeoutError, match=r"within 0\.1s") as failure,
            state.resource_lock(purpose="cli.stop", timeout_s=0.1),
        ):
            pytest.fail("a concurrent stop entered a held resource section")
        assert "ops.pause" in str(failure.value)
        assert "cli.stop" in str(failure.value)

    released = json.loads(state._holder_path(state.lifecycle_lock_path()).read_text())
    assert released["state"] == "released"
    assert released["purpose"] == "ops.pause"
