"""Parent import completes launched SDK composition before functions escape."""

import sys

import ava
from base.host.proc import run_bounded


def test_launched_from_imports_share_boot_and_keep_runtime_resources_cold() -> None:
    code = """
import os
import importlib
import inspect
import json
from base.config import ConfigBoot
from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV
from ava_builtins.plugins.ava_fleet.default_config import FleetConfig
fleet = FleetConfig()
projection = {
    name: getattr(fleet, name)
    for name, info in FleetConfig.model_fields.items()
    if info.json_schema_extra.get("scope") in {"cluster-pinned", "cluster-default"}
}
os.environ[PLUGIN_CLUSTER_CONFIG_ENV] = json.dumps({"ava_fleet": projection})
reads = []
read = ConfigBoot.read_process_environment
def capture(self):
    read(self)
    reads.append(self)
ConfigBoot.read_process_environment = capture
os.environ["AVA_AGENT_ID"] = "7123"
from ava.agents import spawn
from ava.tasks import create
from ava.memory import search
import ava
from ava.sdk_surface import install
assert "label" in inspect.signature(spawn).parameters
assert spawn is ava.agents.spawn
assert create is ava.tasks.create
assert search is ava.memory.search
context = ava.context
installation = install.installed()
assert installation is not None and not installation.faces
assert context.catalog is installation.catalog
assert context.clock_factory is installation.clock_factory
assert len(reads) == 1, reads
boot = reads[0]
boot.set_field("timezone", "Asia/Tokyo")
assert context.require_clock().authoritative_timezone == "Asia/Tokyo"
assert installation.clock_factory().authoritative_timezone == "Asia/Tokyo"
bare = installation.producer.__self__
assert bare is not context.clients
for clients in (context.clients, bare):
    assert clients._sql is None
    assert clients._redis is None
    assert clients._gateway is None
    assert clients._event_pipeline is None
    assert clients._made == {}
assert installation.sampling.worker is None
importlib.reload(ava)
assert ava.context is context
assert install.installed() is installation
assert ava.agents.spawn is spawn
assert len(reads) == 1
"""
    result = run_bounded(
        [sys.executable, "-I", "-c", code],
        cwd=ava.loaded_code_image().source_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_non_agent_import_keeps_existing_unbound_context_contract() -> None:
    code = """
import os
import importlib
os.environ.pop("AVA_AGENT_ID", None)
import ava
from ava.sdk_surface import install
assert getattr(ava, "context", None) is None
assert install.installed() is None
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
manual = AvaContext(clients=ClientSet())
ava.bind_context(manual)
importlib.reload(ava)
assert ava.context is manual
assert install.installed() is None
ava.unbind_context()
ava.bind_host_process()
from types import SimpleNamespace
installed = install.installed
install.installed = lambda: SimpleNamespace(faces=True)
ava.ensure_plugins_loaded()
assert ava.is_host_process()
assert getattr(ava, "context", None) is None
install.installed = installed
error = ValueError("retained explicit load failure")
install.mark_load_failed(error)
importlib.reload(ava)
assert ava.is_host_process()
assert getattr(ava, "context", None) is None
try:
    ava.ensure_plugins_loaded()
except ValueError as caught:
    assert caught is error
else:
    raise AssertionError("explicit loading hid its prior failure")
"""
    result = run_bounded(
        [sys.executable, "-I", "-c", code],
        cwd=ava.loaded_code_image().source_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
