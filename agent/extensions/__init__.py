"""The agent kernel's plugin-state side: the external-attachment state codec and the plugin catalog.

The plugin loader and registry live in `ava.sdk_surface.extensions`; the callers that run agent
code supply `agent.state.plugin_state_schema` to them as the state gate. `plugin_state_codec.py`
is the checkpoint-compatible delta codec, `catalog.py` reads back what loaded plugins declare
(`ava plugins inspect`). The door is docstring-only.
"""
