"""Cold-interpreter SDK telemetry scenarios; never imported by a production entry.

Run as ``python -I _sdk_cold_child.py <scenario>``. ``capture_import_failure`` reads the injected
error class from ``TEST_CAPTURE_ERROR``.
"""

from __future__ import annotations

import importlib.abc
import os
import sys


def cold_import_loads_no_httpx() -> None:
    import base.agents.sdk.telemetry  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert "httpx" not in sys.modules
    assert "httpcore" not in sys.modules


def capture_import_failure() -> None:
    from base.agents.sdk import call_policy, telemetry
    from base.agents.sdk.call_policy import SamplingPolicy

    assert "base.agents.impersonation.manifest" not in sys.modules
    error_type = {"ImportError": ImportError, "RuntimeError": RuntimeError}[
        os.environ["TEST_CAPTURE_ERROR"]
    ]
    failure = error_type("invalid cold capture module")
    executed: list[str] = []
    call_policy.policy = lambda _owner: SamplingPolicy()  # type: ignore[assignment]
    telemetry.emit = lambda *_args, **_kwargs: executed.append("emit")  # type: ignore[assignment]

    class BrokenCapture(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname: str, path: object, target: object = None) -> None:
            if fullname == "base.agents.impersonation.manifest":
                raise failure

    sys.meta_path.insert(0, BrokenCapture())
    try:
        telemetry.run_metered(
            "capture.call",
            lambda: executed.append("body"),
            (),
            {},
            identity={},
            sampling_owner=call_policy.SamplingPolicyOwner(),
        )
    except (ImportError, RuntimeError) as actual:
        assert actual is failure
    else:
        raise AssertionError("capture import failure was hidden")
    assert executed == []


SCENARIOS = {
    "cold_import_loads_no_httpx": cold_import_loads_no_httpx,
    "capture_import_failure": capture_import_failure,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
