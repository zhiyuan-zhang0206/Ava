"""The immutable base of the system prompt + the lazily captured `ava` SDK overview.

Imported lazily by `system_prompt.build_system_prompt`.
"""

from __future__ import annotations

import contextlib
import io

# The SDK itself is introspected (`ava.help(ava)`), not called.
import ava


def _capture_ava_overview() -> str:
    """Emit the `# ava` overview — the public SDK surface as a name + docstring index.

    `ava.help(ava)` renders ava's own docstring plus one entry per public
    top-level namespace — both the static ones (agents / monitor / self /
    schedule / memory / files / shell / skills / ...) and any a plugin
    registered at runtime — each as `from . import X` + that module's docstring.
    Underscore-private members don't appear. Registered namespaces are listed
    too on purpose: a plugin promotes its *members* in its own section, but the
    namespace itself must show here so it's discoverable even if the plugin adds
    no section — otherwise a top-level namespace could silently vanish. This is
    the natural "what's my SDK" index; full per-namespace detail (function
    signatures) stays on demand via `ava.help(ava.X)`.

    Scope is driven by `AVA_SDK_DISABLE`: a disabled namespace is removed from
    `ava` entirely, so it simply doesn't appear here — e.g. SWE-Bench disables
    monitor / schedule / self / agents / skills and the overview narrows to
    match, no framework change needed.

    Duplication note: namespaces a plugin promotes in detail via its own
    declared system prompt section (ava_code → cwd / files / shell) still
    appear here at index level (name + docstring); the plugin section adds the
    function stubs *without* repeating the docstring (help(ava.X) drops a
    submodule target's own docstring — see ava._format_module_stub).
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        # Pass the render parameters explicitly: this is an index of the surface,
        # not per-agent documentation — the per-agent media gating of the members
        # themselves is applied by the expand section (`system_prompt.py`), which
        # knows the agent's model. Keeps the overview byte-stable across models
        # and processes.
        ava.help(ava, hidden_members=frozenset())
    return buf.getvalue()


# At module top-level execution time, ava plugins are not yet loaded (order: import
# _base_prompt → module top → main() → build_graph() → load_extensions()). So capture is
# deferred to the build_system_prompt() call site — by then plugins are loaded
# and each plugin's `contribute()` declaration is in the registry the caller holds.
#
# No cache: build_system_prompt() is called only once in an agent's lifetime
# when the first _claim sees state.messages empty; afterward SystemMessage is
# persisted into state.messages[0] and reused across restart — cache hit rate
# is 1/1, no point.
def _get_ava_overview() -> str:
    return _capture_ava_overview()


# _BASE_SYSTEM_PROMPT is the immutable core of the system prompt.
# The {_AVA_OVERVIEW} placeholder is filled by _get_ava_overview() lazy capture
# the first time build_system_prompt() is called — by then load_extensions() has
# run and all plugin namespaces are visible.
# Plugins add extension content through `PluginContributions.system_prompt_sections`.
_BASE_SYSTEM_PROMPT = """\
You are Ava, an agent that acts by writing Python code — call the
`execute_code(code: str)` tool — each call runs in an ephemeral interpreter. To idle, do not output any
tool calls.

Before using any `ava.*` function, you must explicitly `import ava` in your code.

{_AVA_OVERVIEW}"""
