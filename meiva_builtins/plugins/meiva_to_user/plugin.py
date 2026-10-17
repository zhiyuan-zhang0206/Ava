from __future__ import annotations

__description__ = (
    "Reminds the agent when its to_user panel (the Markdown file shown to the user "
    "beside its messages) grows past the configured soft word limit"
)

from collections.abc import Callable
from pathlib import Path

from base.packages.plugins.extensions import PluginContributions, SdkWrap

from .written import record_panel_write


# meiva:ref meiva_builtins/plugins/meiva_to_user/docs/panel-write-check.meiva.okf.md#panel-write-observation
def wrapped_write(inner: Callable[..., None], path: str | Path, content: str) -> None:
    result = inner(path, content)
    record_panel_write(path)
    return result


def wrapped_append(inner: Callable[..., None], path: str | Path, content: str) -> None:
    result = inner(path, content)
    record_panel_write(path)
    return result


def wrapped_edit(
    inner: Callable[..., None], path: str | Path, old: str, new: str, *, replace_all: bool = False
) -> None:
    result = inner(path, old, new, replace_all=replace_all)
    record_panel_write(path)
    return result


def contribute() -> PluginContributions:
    return PluginContributions(
        sdk_wraps=(
            SdkWrap("files.write", wrapped_write),
            SdkWrap("files.append", wrapped_append),
            SdkWrap("files.edit", wrapped_edit),
        ),
    )
