"""Plugin discovery, loading and registry: what a process imports from the plugin directories.

`loader.py` imports each enabled plugin's faces and installs what they declare,
`registry.py` builds the `ExtensionRegistry` from the faces a process loaded, `discovery.py`
answers which plugins are enabled and where their faces live, and `plugin_loader.py` is the
by-path module loader they all drive. The door defines only `StateGate`.
"""

from collections.abc import Callable
from typing import Any

# What a plugin's state declarations must pass when an agent-runtime face is loaded: the agent
# kernel's `agent.state.plugin_state_schema`, given the one-plugin `ExtensionRegistry` it checks.
# The loader sits below the kernel, so the check arrives as an argument from the caller that
# runs agent code; the argument is typed loosely here to keep `import meiva` off the plugin stack.
StateGate = Callable[[Any], object]
