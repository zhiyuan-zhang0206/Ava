"""What the model is told: the system prompt and the standing context notes.

`system_prompt` assembles the base prompt plus plugin sections, `capabilities` renders the
`# Capabilities` index, `context_notes` is the registry of standing notes. `_base_prompt`
and `_codeact` are the prompt's private text sources. The door is docstring-only so
importing one submodule does not drag in the rest.
"""
