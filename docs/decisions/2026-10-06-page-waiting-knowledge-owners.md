# Page, waiting, and model-speed knowledge owners

## Decision

Keep SDK call contracts in docstrings and runtime structure in component docs.
Use skills for selection and composition beyond a single call. Use general
frontend skills for design rather than maintaining Ava-specific visual guidance.

Replace `ava-ui` with the compact `ava-guide/pages` composition guide and its
frontend resources. Its reply templates use the authenticated page's existing
same-origin user message path. Do not introduce a frontend SDK, browser agent
credentials, page tokens, or an application backend for each generated page.
The agent id routes the input; it is not proof of authority. A submitted choice
is interpreted within the task's existing authorization.

Remove `ava-watcher` as an independent skill. Its API contracts belong to
`ava.watcher`; bounded waiting, probe quality, recovery, and cleanup belong to
`ava-being-a-long-running-agent`. Move the idle-wait reference script there.

Remove `ava-ultra-speed`. Shared lifecycle and fleet rules already cover useful
reporting and task completion. Presets choose registered models and feedback
configuration independently; they do not carry arbitrary prompt text or require
short repeated polls. Document removal of stale preload entries from stored
presets before creating new agents.

## Why

Separate manuals had duplicated runtime facts and allowed the page/authentication
instructions to drift from the current platform. Moving whole manuals into Guide
would preserve that duplication. A new browser package would only wrap transport
without resolving ownership. Keep the existing mechanism and give each kind of
knowledge one owner, with small composition guides where resources need them.

## Activation

The source cleanup does not alter a running deployment or stored presets.
Repo-native package copies follow the explicit skill-update procedure. External
clients receive page resources as part of the complete managed Ava Guide package.
