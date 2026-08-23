# Plugin boundary audit fixes

This batch closes audit findings at the boundaries where a flexible extension
surface meets a system-wide invariant.

Memory authoring now has one dedicated API that derives an absolute target from
the agent identity and selected store. Routing it through cwd-aware file
operations was rejected because a coding agent's logical project directory is
not the ownership boundary for its durable memory. The API owns generated
frontmatter and the index pointer together so an entry cannot be written into a
correct directory while silently disappearing from its injected index.

Framework configuration keys are validated when a preset is stored, while
unknown keys remain opaque. Loading the plugin registry in the gateway merely
to validate every possible plugin field would couple this CRUD boundary to an
agent-runtime concern; accepting unknown keys preserves plugin extensibility,
and rejecting known non-per-agent framework fields makes cluster-consistency
errors immediate.

Native LGTM jobs are keyed by the cluster home slug and retire both fixed-label
legacy jobs and another home's job on the marked singleton host. Coexistence
was rejected because those jobs bind fixed host ports; preserving either stale
job would make ownership depend on launch order.

The remaining guards make plugin auto-merge ordering deterministic and make
the NoteTag frontend dispatch contract bidirectional, so removed backend tags
cannot linger as unreachable frontend branches.
