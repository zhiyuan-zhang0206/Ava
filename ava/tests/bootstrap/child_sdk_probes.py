"""Child process for the SDK import and boot tests: each scenario needs a fresh interpreter.

Usage: `child_sdk_probes.py <scenario> [arg]`. Imports live inside the scenarios because the
order of the environment edits against the first `import ava` is part of what is tested.
"""

import sys


def config_lite() -> None:
    import ava  # noqa: F401  # pyright: ignore[reportUnusedImport]
    import base.config as c

    st = c.boot_state()  # pyright: ignore[reportAttributeAccessIssue]
    print(
        "AVA",
        st["mode"],
        st["upgrades"],
        "pydantic_settings" in sys.modules,
        "base.config.base" in sys.modules,
    )


def no_context_for_bare_script() -> None:
    import ava

    print(hasattr(ava, "context"))


def telemetry_overflow() -> None:
    import threading
    from datetime import UTC, datetime

    import ava  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from base import telemetry

    entered = threading.Event()
    release = threading.Event()

    def write(events: object) -> None:
        entered.set()
        assert release.wait(5)

    pipe = telemetry.EventPipeline(writer=write, batch_size=1, queue_maxsize=1)  # pyright: ignore[reportArgumentType]
    event = telemetry.Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="standalone",
        category="telemetry",
        event_name="sdk_call",
        level="info",
        source="system",
        target_agent_id=None,
    )
    try:
        pipe.enqueue(event)
        assert entered.wait(5)
        pipe.enqueue(event)
        pipe.enqueue(event)
    finally:
        release.set()
        assert pipe.stop(timeout=5).status is telemetry.DrainStatus.COMPLETED


def plain_import_sdk_events(target: str) -> None:
    import json

    import ava
    from base import telemetry
    from base.agents.sdk import call_policy as sdk_call_policy

    sdk_call_policy.policy = lambda _owner: sdk_call_policy.SamplingPolicy()  # pyright: ignore[reportAttributeAccessIssue]
    rows: list[dict[str, object]] = []

    def capture(*args: object, producer: object, machine_reader: object, **kwargs: object) -> None:
        assert callable(producer)
        assert callable(machine_reader)
        assert machine_reader() == ava.context.require_machine().name()
        rows.append(kwargs)

    telemetry.emit = capture  # pyright: ignore[reportAttributeAccessIssue]
    assert ava.files.read(target) == "hello"
    print(json.dumps(rows))


def agent_profile_owner_url_guard() -> None:
    from base.config import settings

    try:
        _ = settings.data_plane.db_url
    except Exception as exc:
        print(
            type(exc).__name__,
            "agent-profile processes must receive a runner-class" in str(exc),
        )


def non_agent_import_unbound_context() -> None:
    import importlib
    import os
    from types import SimpleNamespace

    os.environ.pop("AVA_AGENT_ID", None)
    import ava
    from ava.sdk_surface import install, installation, process_context

    assert getattr(ava, "context", None) is None
    assert installation.installed() is None
    from base.agents.context import AvaContext
    from base.agents.context.clients import ClientSet

    manual = AvaContext(clients=ClientSet())
    process_context.bind_context(manual)
    importlib.reload(ava)
    assert ava.context is manual
    assert installation.installed() is None
    process_context.unbind_context()
    process_context.bind_host_process()
    installed = installation.installed
    installation.installed = lambda: SimpleNamespace(faces=True)  # pyright: ignore[reportAttributeAccessIssue]
    ava.ensure_plugins_loaded()
    assert process_context.is_host_process()
    assert getattr(ava, "context", None) is None
    installation.installed = installed
    error = ValueError("retained explicit load failure")
    install.mark_load_failed(error)
    importlib.reload(ava)
    assert process_context.is_host_process()
    assert getattr(ava, "context", None) is None
    try:
        ava.ensure_plugins_loaded()
    except ValueError as caught:
        assert caught is error
    else:
        raise AssertionError("explicit loading hid its prior failure")


SCENARIOS = {
    "config_lite": config_lite,
    "no_context_for_bare_script": no_context_for_bare_script,
    "telemetry_overflow": telemetry_overflow,
    "plain_import_sdk_events": plain_import_sdk_events,
    "agent_profile_owner_url_guard": agent_profile_owner_url_guard,
    "non_agent_import_unbound_context": non_agent_import_unbound_context,
}

SCENARIOS[sys.argv[1]](*sys.argv[2:])  # pyright: ignore[reportCallIssue]
