"""Regression guards for the sysprompt verbosity audit (PR #840).

The system prompt renders plugin-wrapped SDK functions through the
wrapper's docstring (ava_code wraps `shell.run`, ava_fleet wraps
`agents.spawn`), so trimming the core docstring alone is a no-op for
those two surfaces. These tests load the real plugin and assert the
trimmed name-restatement sentences stay out of the rendered help stub
— each assertion goes red under the pre-audit wrapper text.
"""

import io
from collections.abc import Iterator
from contextlib import redirect_stdout

import pytest

import ava
from ava.sdk_surface import install
from base.packages.plugins.extensions import ExtensionRegistry


def _render_help(target: object) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ava.help(target)
    return buf.getvalue()


@pytest.fixture
def _load_ava_code_plugin(
    monkeypatch: pytest.MonkeyPatch, model_installation: install.Installation
) -> Iterator[None]:
    from ava_builtins.plugins.ava_code import plugin
    from base.agents.sdk import call_policy

    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)

    install.install(
        ExtensionRegistry((("ava_code", plugin.contribute()),)),
        catalog=model_installation.require_catalog(),
        authority=model_installation.authority,
    )

    yield

    install.uninstall()


@pytest.fixture
def _load_ava_fleet_plugin(
    monkeypatch: pytest.MonkeyPatch, model_installation: install.Installation
) -> Iterator[None]:
    from ava_builtins.plugins.ava_fleet import plugin
    from base.agents.sdk import call_policy

    monkeypatch.setattr(call_policy, "policy", call_policy.SamplingPolicy)

    install.install(
        ExtensionRegistry((("ava_fleet", plugin.contribute()),)),
        catalog=model_installation.require_catalog(),
        authority=model_installation.authority,
    )

    yield

    install.uninstall()


def test_shell_run_wrapper_renders_trimmed_docstring(_load_ava_code_plugin: None) -> None:
    """The ava_code wrap of shell.run carries its own docstring; the prompt
    renders that one, so the trimmed name restatement must be gone there."""
    out = _render_help(ava.shell.run)
    assert "Run a shell command and return its stdout." not in out
    assert "Non-zero exit does not" in out


def test_spawn_wrapper_renders_trimmed_docstring(_load_ava_fleet_plugin: None) -> None:
    """The ava_fleet wrap of agents.spawn carries its own docstring (label
    support); assert the pre-audit first line stays out of the rendered stub."""
    out = _render_help(ava.agents.spawn)
    assert "Start a new agent and return its id. Does not block." not in out
    assert "does not block" in out
