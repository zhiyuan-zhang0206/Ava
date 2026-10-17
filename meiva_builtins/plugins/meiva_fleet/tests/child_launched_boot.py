from typing import Any, cast


def _assert_runtime_resources_cold(context: Any, installation: Any) -> None:
    assert installation.producer is not None
    bare = installation.producer.__self__
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

    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV
    from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig

    fleet = FleetConfig()
    projection = {
        name: getattr(fleet, name)
        for name, info in FleetConfig.model_fields.items()
        if cast("dict[str, object]", info.json_schema_extra).get("scope")
        in {"cluster-pinned", "cluster-default"}
    }
    os.environ[PLUGIN_CLUSTER_CONFIG_ENV] = json.dumps({"meiva_fleet": projection})


def _record_boot_reads() -> list[Any]:
    from base.config import ConfigBoot

    reads: list[ConfigBoot] = []
    read = ConfigBoot.read_process_environment

    def capture(self: ConfigBoot) -> None:
        read(self)
        reads.append(self)

    ConfigBoot.read_process_environment = capture
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
    os.environ["MEIVA_AGENT_ID"] = "7123"
    import meiva
    from meiva.agents import spawn
    from meiva.sdk_surface.installation import installed

    create = importlib.import_module("meiva.tasks").create
    search = importlib.import_module("meiva.memory").search

    assert "label" in inspect.signature(spawn).parameters
    assert spawn is meiva.agents.spawn
    assert create is meiva.tasks.create
    assert search is meiva.memory.search
    context = meiva.context
    installation = installed()
    assert installation is not None
    _assert_shared_boot(context, installation, reads)
    _assert_runtime_resources_cold(context, installation)
    assert installation.sampling.worker is None
    importlib.reload(meiva)
    assert meiva.context is context
    assert installed() is installation
    assert meiva.agents.spawn is spawn
    assert len(reads) == 1


launched_imports_share_boot()
