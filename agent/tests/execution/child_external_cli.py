"""An external CLI attached to a lease: reads one file through the public SDK, then exits.

`python -I -X utf8 child_external_cli.py <sample file>`; run by test_telemetry_otlp_exit_delivery_agent,
which watches the OTLP receiver for the SDK-call events this process leaves behind.
"""

import os
import sys
from pathlib import Path
from typing import Any

import ava
from agent.extensions import plugin_state_codec
from agent.state import build_agent_state, plugin_state_schema
from ava import impersonation
from ava.impersonation import attachment as attachment_module
from base.packages.plugins.loading.registry import build_registry

SAMPLE = sys.argv[1]
LEASE: dict[str, Any] = {
    "id": "lease",
    "session_id": 0,
    "agent_id": 424242,
    "machine": "external-cli-probe",
    "status": "active",
    "delta_version": 0,
    "applied_version": 0,
    "plugin_delta": [],
    "automatic": False,
    "event_delivery_protocol_version": None,
}


def require_active(_db: Any, _lease_id: Any, *, local_machine: str) -> dict[str, Any]:
    assert local_machine == "external-cli-probe"
    return dict(LEASE)


def load_snapshot(_agent_id: Any, **_kwargs: Any) -> tuple[Any, dict[str, Any], None]:
    state: Any = build_agent_state(build_registry(validate_state=plugin_state_schema))()
    state.ava_code__cwd = str(Path(SAMPLE).parent)
    return state, {}, None


attachment_module.control.require_active = require_active  # pyright: ignore[reportAttributeAccessIssue]
os.environ["AVA_MACHINE_NAME"] = "external-cli-probe"
plugin_state_codec.load_snapshot = load_snapshot  # pyright: ignore[reportAttributeAccessIssue]

with impersonation.attach("lease"):
    assert ava.files.read(SAMPLE) == "tail event"
