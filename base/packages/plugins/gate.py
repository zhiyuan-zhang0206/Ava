"""The manifest gate: a plugin's `ava-plugin.json` and what it declares must agree.

A plugin without a manifest has nothing to disagree with. With one, each key the caller gates must
list exactly the identifiers the plugin's `PluginContributions` provides on that surface: a declared
surface it does not deliver, and a delivered one it does not declare, both refuse. Each process
gates only the keys of the faces it loads (the agent registry the hooks and system prompt sections,
the data registry the metrics and inspector widgets), so one manifest is never judged against a
declaration the process cannot see.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import cast

from base.packages.plugins.extensions import PluginContributions
from base.packages.plugins.manifest import load_manifest

_CONFIG_IDENTIFIER = "<declared>"


class ContributionMismatch(Exception):  # noqa: N818 — a load failure of one plugin, not an error class family
    """A plugin's `ava-plugin.json` and its declaration disagree."""


def check_manifest(
    name: str, plugin_dir: Path, contributions: PluginContributions, keys: Iterable[str]
) -> None:
    """Refuse a declaration that disagrees with the plugin's manifest on `keys`.

    The key is also the attribution surface id (`Contribution.surface`).
    """
    manifest = load_manifest(plugin_dir)
    if manifest is None:
        return
    gated = tuple(keys)
    delivered: dict[str, set[str]] = {key: set() for key in gated}
    for record in contributions.as_records(name):
        if record.surface in delivered:
            # `contributions.config` is an object (one config class or none), so it folds to a single
            # identifier on both sides rather than comparing class names, which a rename would break.
            delivered[record.surface].add(
                _CONFIG_IDENTIFIER if record.surface == "config" else record.identifier
            )
    problems: list[str] = []
    for key in gated:
        declared = cast(object, manifest.contributions.get(key))
        if key == "config":
            declared_ids = {_CONFIG_IDENTIFIER} if isinstance(declared, dict) else set[str]()
        else:
            declared_ids = (
                {str(item) for item in cast(list[object], declared)}
                if isinstance(declared, list)
                else set[str]()
            )
        for identifier in sorted(declared_ids - delivered[key]):
            problems.append(f"{key}: {identifier!r} is declared but the plugin does not provide it")
        for identifier in sorted(delivered[key] - declared_ids):
            problems.append(f"{key}: {identifier!r} is provided but not declared")
    if problems:
        raise ContributionMismatch(
            f"plugin {name!r}: ava-plugin.json and its declaration disagree — "
            + "; ".join(problems)
        )
