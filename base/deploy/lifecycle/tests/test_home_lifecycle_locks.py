# pyright: reportUnknownArgumentType=warning, reportUnknownLambdaType=warning
"""Home lifecycle mutex: a bounded wait that names the last holder."""

from __future__ import annotations

import json

import pytest

from base.deploy.lifecycle import home_lifecycle_locks as state
from base.native_process.os_platform import LockTimeoutError


def test_a_bounded_wait_names_the_last_holder(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state.base.paths, "ava_home", lambda: tmp_path)

    with state.resource_lock(purpose="ops.pause"):
        resource = json.loads(state._holder_path(state.lifecycle_lock_path()).read_text())
        assert resource["state"] == "held"
        assert resource["purpose"] == "ops.pause"

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
