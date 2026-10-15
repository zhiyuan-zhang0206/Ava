import json
from pathlib import Path

import pytest

from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.gate import ContributionMismatch, check_manifest
from base.packages.plugins.loading.registry import FACE_KEYS


def preparation(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    return "Save new findings before compaction."


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("provided", [True, False])
def test_manifest_must_match_compact_preparation(
    tmp_path: Path, declared: bool, provided: bool
) -> None:
    (tmp_path / "ava-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "example",
                "version": "1.0.0",
                "engines": {"ava": ">=2026.10.1"},
                "contributions": {"compactReminderSections": ["preparation"] if declared else []},
            }
        )
    )
    contribution = PluginContributions(
        compact_reminder_sections=(preparation,) if provided else (),
    )
    keys = FACE_KEYS["agent_runtime"]
    if declared != provided:
        with pytest.raises(ContributionMismatch, match="compactReminderSections"):
            check_manifest("example", tmp_path, contribution, keys)
    else:
        check_manifest("example", tmp_path, contribution, keys)
    records = contribution.as_records("example")
    assert [(r.surface, r.identifier) for r in records] == (
        [("compactReminderSections", "preparation")] if provided else []
    )


def test_merged_runtime_declarations_keep_reminder_order() -> None:
    first = PluginContributions(compact_reminder_sections=(preparation,))
    merged = PluginContributions().merged(first)
    registry = ExtensionRegistry((("example", merged),))
    assert list(registry.compact_reminder_sections()) == [("example", preparation)]
    assert list(ExtensionRegistry().compact_reminder_sections()) == []
