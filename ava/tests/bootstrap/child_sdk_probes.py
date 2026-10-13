"""Child process for the SDK import and boot tests: each scenario needs a fresh interpreter.

Usage: `child_sdk_probes.py <scenario> [arg]`. Imports live inside the scenarios because the
order of the environment edits against the first `import ava` is part of what is tested.
"""

import sys
from collections.abc import Callable
from typing import Any, cast


def config_lite() -> None:
    import ava  # noqa: F401  # pyright: ignore[reportUnusedImport]
    import base.config as c

    st = c._boot_state()  # pyright: ignore[reportAttributeAccessIssue]
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


def _assert_runtime_resources_cold(context: Any, installation: Any) -> None:
    assert installation.producer is not None
    bare = installation.producer.__self__  # pyright: ignore[reportFunctionMemberAccess]
    assert bare is not context.clients
    for clients in (context.clients, bare):
        assert clients._sql is None
        assert clients._redis is None
        assert clients._gateway is None
        assert clients._event_pipeline is None
        assert clients._made == {}


def _pin_fleet_cluster_config() -> None:
    import json
    import os

    from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

    fleet = FleetConfig()
    projection = {
        name: getattr(fleet, name)
        for name, info in FleetConfig.model_fields.items()
        if cast("dict[str, object]", info.json_schema_extra).get("scope")
        in {"cluster-pinned", "cluster-default"}
    }
    os.environ[PLUGIN_CLUSTER_CONFIG_ENV] = json.dumps({"ava_fleet": projection})


def _record_boot_reads() -> list[Any]:
    from base.config import ConfigBoot

    reads: list[ConfigBoot] = []
    read = ConfigBoot.read_process_environment

    def capture(self: ConfigBoot) -> None:
        read(self)
        reads.append(self)

    ConfigBoot.read_process_environment = capture  # pyright: ignore[reportAttributeAccessIssue]
    return reads


def _assert_shared_boot(context: Any, installation: Any, reads: list[Any]) -> None:
    assert not installation.faces
    assert context.catalog is installation.catalog
    assert context.clock_factory is installation.clock_factory
    assert len(reads) == 1, reads
    boot = reads[0]
    boot.set_field("timezone", "Asia/Tokyo")
    assert context.require_clock().authoritative_timezone == "Asia/Tokyo"
    assert installation.clock_factory is not None
    assert installation.clock_factory().authoritative_timezone == "Asia/Tokyo"


def launched_imports_share_boot() -> None:
    import importlib
    import inspect
    import os

    _pin_fleet_cluster_config()
    reads = _record_boot_reads()
    os.environ["AVA_AGENT_ID"] = "7123"
    import ava
    from ava.agents import spawn
    from ava.memory import search  # pyright: ignore[reportMissingImports]
    from ava.sdk_surface.installation import installed
    from ava.tasks import create  # pyright: ignore[reportMissingImports]

    assert "label" in inspect.signature(spawn).parameters
    assert spawn is ava.agents.spawn
    assert create is ava.tasks.create
    assert search is ava.memory.search
    context = ava.context
    installation = installed()
    assert installation is not None
    _assert_shared_boot(context, installation, reads)
    _assert_runtime_resources_cold(context, installation)
    assert installation.sampling.worker is None
    importlib.reload(ava)
    assert ava.context is context
    assert installed() is installation
    assert ava.agents.spawn is spawn
    assert len(reads) == 1


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
    from ava.sdk_surface import agent_identity, process_context

    os.environ["AVA_AGENT_ID"] = "5"
    process_context.bind_host_process()
    assert process_context.is_host_process()
    assert agent_identity.is_launched_child() is False
    assert getattr(ava, "context", None) is None
    _expect_refusal(lambda: ava.external.attach("must-not-be-read"), "native agent runtime")
    _expect_refusal(
        lambda: process_context.bind_context(process_context.launched_context()),  # pyright: ignore[reportArgumentType]
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
    "config_lite": config_lite,
    "no_context_for_bare_script": no_context_for_bare_script,
    "telemetry_overflow": telemetry_overflow,
    "plain_import_sdk_events": plain_import_sdk_events,
    "agent_profile_owner_url_guard": agent_profile_owner_url_guard,
    "launched_imports_share_boot": launched_imports_share_boot,
    "non_agent_import_unbound_context": non_agent_import_unbound_context,
    "host_refuses_env_context_and_attachment": host_refuses_env_context_and_attachment,
    "host_rejects_inherited_child_identity": host_rejects_inherited_child_identity,
}

SCENARIOS[sys.argv[1]](*sys.argv[2:])  # pyright: ignore[reportCallIssue]
