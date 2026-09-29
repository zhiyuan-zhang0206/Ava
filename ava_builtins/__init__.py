"""Ava built-in capabilities — skills, MCP servers, and plugins that ship with the core.

Naming: `skills/` holds only skill directories, kebab-case and matching each
skill's `name` (the Agent Skills standard); `plugins/` and `mcps/` hold Python
packages, snake_case. A skill's own code lives only in its own `scripts/` —
never reached from another skill or from a plugin by file path.
"""
