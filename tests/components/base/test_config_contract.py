from __future__ import annotations


def test_frontend_config_group_keys_match_backend_aliases() -> None:
    import re
    from pathlib import Path

    from base.config import field_alias_map

    groups_ts = (
        Path(__file__).resolve().parents[2].parent
        / "ui"
        / "web"
        / "src"
        / "app"
        / "control"
        / "_config_groups.ts"
    )
    assert groups_ts.exists(), f"missing frontend map: {groups_ts}"
    src = groups_ts.read_text()
    block_start = src.index("export const GROUP_ENV_VARS")
    block_end = src.index("};", block_start) + 2
    keys = re.findall(r'"([A-Z][A-Z0-9_]*)"', src[block_start:block_end])
    assert len(keys) > 100, f"parse looks wrong — only {len(keys)} keys extracted"

    aliases = set(field_alias_map().values())
    dead = [k for k in keys if k not in aliases]
    assert not dead, (
        f"frontend GROUP_ENV_VARS keys with no backend field: {dead} — "
        f"remove them or fix the spelling (a dead key renders nothing and the "
        f"real field falls into a default bucket)"
    )
