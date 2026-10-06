---
name: pages
description: Combines Ava page publishing, reusable display templates, and user replies for a task. Use when presenting a rich artifact or collecting a choice, confirmation, or form through an Ava page; use a general frontend skill for visual design.
---

# Pages and user input

Use a page when an artifact or structured choice is clearer than a chat message.
Use an installed general frontend skill for layout, styling, accessibility, and
responsive design. This guide covers how the page participates in an Ava task.

## Publish and continue the task

1. Decide whether the page displays an artifact or asks for input. For input,
   state the question, choices, and the action each choice authorizes.
2. Build the frontend with the resources below or your existing frontend tools.
   Keep assets relative to the page directory. Render Markdown to HTML.
3. Read `ava.help(ava.ui)` for publishing and lifecycle contracts. Static output
   uses `ava.ui.serve`; an existing server uses `ava.ui.show`. Share the returned
   page URL so the user opens the authenticated platform page.
4. For a reply, use the [page reply resource](widgets/ava_reply/README.md).
   Record the question and awaited result, then end the turn. A submitted result
   arrives as user input; interpret it within the task's existing authority.
5. Close the page when it is no longer needed. A page is an interaction surface,
   not an independent source of permissions or an ongoing monitor.

Human and external contributors can read the runtime contract in the source
checkout at `ava/docs/ui.ava.okf.md` and `ava/ui.py`. The templates are ordinary
frontend resources; display-only components also work outside Ava.

## Resources

Paths below are relative to this skill directory. Ava installs it at
`$AVA_HOME/skills/ava-guide/pages/`; external clients receive it inside their
installed `ava-guide` package.

| Need | Resource |
|---|---|
| Render Markdown, math, and highlighted code | [Markdown](widgets/markdown/README.md) |
| Display media with a synchronized transcript | [Transcript](widgets/transcript/README.md) |
| Collect one or several selections | [Choice](widgets/choice/README.md) |
| Ask for a specific approval or rejection | [Confirm](widgets/confirm/README.md) |
| Collect named fields | [Form](widgets/form/README.md) |
| Compare artifacts and choose one | [Compare](widgets/compare/README.md) |
| Create a static page | [Single HTML](starters/single_html/README.md) |
| Build a React page | [React](starters/react_vite/README.md) |
