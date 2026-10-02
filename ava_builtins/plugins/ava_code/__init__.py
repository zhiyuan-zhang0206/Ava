"""The ava-code plugin package door.

`project_skill_roots` is the one name consumed outside the plugin: the ops
`agent_skill_view` op resolves the project-local skill roots an agent's
working directory would mount.
"""

from ._walk import project_skill_roots

__all__ = ["project_skill_roots"]
