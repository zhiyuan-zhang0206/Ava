import ast
import inspect

import pytest

from base.daemon.schedules.watcher import build_at_script, build_cron_script
from meiva import gateway_client
from meiva.sdk_surface import agent_identity

_EXPECTED_USES = {
    ("meiva", "gateway_client"): {"send_message"},
    ("meiva.sdk_surface", "agent_identity"): {"agent_id"},
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


def _meiva_uses(source: str) -> dict[tuple[str, str], set[str]]:
    tree = ast.parse(source)
    bound: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] == "meiva"
        ):
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
def test_a_generated_watcher_imports_and_uses_only_the_pinned_meiva_names(name: str) -> None:
    assert _meiva_uses(_rendered_scripts()[name]) == _EXPECTED_USES


def test_the_pinned_names_exist_with_the_call_shape_the_generated_code_uses() -> None:
    wake = inspect.signature(gateway_client.send_message)
    wake.bind(7, content="wake", source="watcher:12")
    assert inspect.signature(agent_identity.agent_id).bind().arguments == {}
