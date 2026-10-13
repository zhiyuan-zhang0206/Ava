"""Child process: the shared agent host's startup refuses a launched-agent SDK posture.

Usage: `child_sdk_host_posture.py <scenario>`. Each scenario needs a fresh interpreter because
the order of the environment edits against the first `import ava` is part of what is tested.
"""

import sys
from collections.abc import Callable


def _expect_refusal(action: Callable[[], object], fragment: str) -> None:
    try:
        action()
    except RuntimeError as exc:
        assert fragment in str(exc)
    else:
        raise AssertionError(f"the host did not refuse: {fragment}")


def host_refuses_env_context_and_attachment() -> None:
    import os
    import runpy

    os.environ.pop("AVA_AGENT_ID", None)
    import ava
    from base import config

    class BootStoppedError(RuntimeError):
        pass

    def stop_boot(self: object) -> None:
        raise BootStoppedError("after SDK posture")

    config.ConfigBoot.ensure_eager = stop_boot  # pyright: ignore[reportAttributeAccessIssue]
    try:
        runpy.run_module("services.agent_runner.agent_host.daemon", run_name="__main__")
    except BootStoppedError:
        stopped = True
    else:
        stopped = False
    assert stopped, "boot did not stop"
    from ava import boot
    from ava.sdk_surface import agent_identity, process_context

    os.environ["AVA_AGENT_ID"] = "5"
    process_context.bind_host_process()
    assert process_context.is_host_process()
    assert agent_identity.is_launched_child() is False
    assert getattr(ava, "context", None) is None
    _expect_refusal(lambda: ava.impersonation.attach("must-not-be-read"), "native agent runtime")
    _expect_refusal(
        lambda: process_context.bind_context(boot.launched_context()),  # pyright: ignore[reportArgumentType]
        "shared agent host",
    )
    _expect_refusal(process_context.unbind_context, "startup posture")


def host_rejects_inherited_child_identity() -> None:
    import os
    import runpy

    os.environ["AVA_AGENT_ID"] = "5"
    try:
        runpy.run_module("services.agent_runner.agent_host.daemon", run_name="__main__")
    except RuntimeError as exc:
        assert "cannot inherit a launched-agent identity" in str(exc)
    else:
        raise AssertionError("host accepted inherited child identity")
    assert "ava" not in sys.modules


SCENARIOS = {
    "host_refuses_env_context_and_attachment": host_refuses_env_context_and_attachment,
    "host_rejects_inherited_child_identity": host_rejects_inherited_child_identity,
}

SCENARIOS[sys.argv[1]]()
