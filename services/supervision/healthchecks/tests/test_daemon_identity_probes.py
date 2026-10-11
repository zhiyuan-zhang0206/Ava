"""The roster's identity probe for each standard daemon asks the right questions.

`probe_daemon` believes a 200 only when the body's `name` and `home` match and its
`pid` equals this unit's pidfile, so a probe built from the wrong name, URL or pidfile
would accept another daemon as healthy or reject the right one forever. Root probes
every standard `/healthz` daemon through `ops.roster.healthz.daemon_identity`; this pins
that the three facts reaching `probe_daemon` are the daemon's own, for every such entry
in the roster (the per-module copies of this check died with the modules).
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast

from base import paths
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import DaemonProbe, probe_daemon
from ops import roster
from ops.roster.service_spec import ServiceSpec
from services.agent_runner.page_server import daemon as page_server
from services.agent_runner.page_server import resources as page_resources


def _standard_daemons() -> list[ServiceSpec]:
    return [s for s in roster.build_services() if s.health_name is not None]


def _inner_probe(spec: ServiceSpec) -> partial[DaemonProbe]:
    """The standard daemon declares its protocol probe directly."""
    return cast("partial[DaemonProbe]", spec.identity_probe)


def test_every_standard_daemon_probe_is_scoped_to_its_name_url_and_pidfile() -> None:
    specs = _standard_daemons()
    assert specs

    for spec in specs:
        name = cast("str", spec.health_name)
        probe = _inner_probe(spec)
        assert probe.func is probe_daemon, spec.session
        assert probe.args == (
            name,
            f"http://localhost:{ServiceEndpoints.from_settings().of(name).health_port}/healthz",
        ), spec.session
        assert probe.keywords == {"pidfile": ServiceEndpoints.from_settings().of(name).pidfile}, (
            spec.session
        )


def test_every_standard_daemon_records_the_pidfile_its_probe_reads(
    process_config: ConfigBoot,
) -> None:
    """The daemon writes the pidfile of its health name and the roster probe cross-checks
    that same file, so the two cannot drift apart (a hyphenated `agent-host.pid`
    once sat beside the underscored health name)."""
    for spec in _standard_daemons():
        module = importlib.import_module(spec.cmd.rsplit(" ", 1)[1])
        # Page-server's resource operations live in their actual public owner.
        operation = page_resources.pidfile if module is page_server else module._pidfile
        parameters = inspect.signature(operation).parameters
        if not parameters:
            actual = cast("Callable[[], Path]", operation)()
        else:
            assert set(parameters) == {"config", "runtime"}, spec.session
            assert all(p.default is inspect.Parameter.empty for p in parameters.values()), (
                spec.session
            )
            assert parameters["runtime"].kind is inspect.Parameter.KEYWORD_ONLY, spec.session
            actual = operation(config=process_config, runtime=paths.run_dir())
        assert (
            actual == ServiceEndpoints.from_settings().of(cast("str", spec.health_name)).pidfile
        ), spec.session
