"""The `ava` names the generated watcher programs import are a contract with programs already
written to disk.

The cron and one-shot watchers the platform generates import `ava.gateway_client` and
`ava.sdk_surface.agent_identity` and call `send_message` / `agent_id` to wake their agent. A
generated program is read once when it launches, but an already-running or restored watcher
(a renewed session, a restore kit) keeps importing these names for up to a week, so they cannot
move without regenerating those programs first. This test renders every template, reads which
`ava` modules and attributes the generated code uses, and checks that exactly those still exist
with the call shape the generated code relies on.
"""

import ast
import inspect

import pytest

from ava import gateway_client
from ava.sdk_surface import agent_identity
from base.daemon.schedules.watcher import build_at_script, build_cron_script

_EXPECTED_USES = {
    ("ava", "gateway_client"): {"send_message"},
    ("ava.sdk_surface", "agent_identity"): {"agent_id"},
}


def _rendered_scripts() -> dict[str, str]:
    return {
        "one-shot, cluster zone": build_at_script(
            when_iso="2030-01-01T00:00:00+00:00", message="wake", timezone="UTC"
        ),
        "one-shot, host zone": build_at_script(
            when_iso="2030-01-01T00:00:00+00:00", message="wake", timezone=None
        ),
        "cron": build_cron_script(
            expr="0 9 * * *", message="wake", timezone="UTC", end_time_iso=None
        ),
    }


def _ava_uses(source: str) -> dict[tuple[str, str], set[str]]:
    """`{(module, imported name): attributes used}` for every `from ava... import name`."""
    tree = ast.parse(source)
    bound: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "ava":
            for alias in node.names:
                bound[alias.asname or alias.name] = (node.module, alias.name)
    uses: dict[tuple[str, str], set[str]] = {key: set() for key in bound.values()}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in bound
        ):
            uses[bound[node.value.id]].add(node.attr)
    return uses


@pytest.mark.parametrize("name", sorted(_rendered_scripts()))
def test_a_generated_watcher_imports_and_uses_only_the_pinned_ava_names(name: str) -> None:
    assert _ava_uses(_rendered_scripts()[name]) == _EXPECTED_USES


def test_the_pinned_names_exist_with_the_call_shape_the_generated_code_uses() -> None:
    wake = inspect.signature(gateway_client.send_message)
    wake.bind(7, content="wake", source="watcher:12")  # agent_id positional, the rest by keyword
    assert inspect.signature(agent_identity.agent_id).bind().arguments == {}
