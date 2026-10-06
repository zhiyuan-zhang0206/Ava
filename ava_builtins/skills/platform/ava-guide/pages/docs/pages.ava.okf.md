---
type: doc
title: Ava Guide pages — Publish artifacts and collect user input
description: Task composition and frontend resources for authenticated Ava pages; SDK contracts own publishing and lifecycle, while general frontend skills own visual design.
tags:
- extensions
- agent-instruction
---

# Ava Guide pages

The [pages guide](../SKILL.md) connects a task's artifact or question to page
publishing, user input, and continuation. Its widgets and starters are frontend
resources, not a separate visual-design methodology.

The source owner for publishing and lifecycle is `ava/ui.py`, with runtime
structure described at `ava/docs/ui.ava.okf.md`. The reply resource documents the
existing authenticated browser message path. It does not introduce a frontend
SDK, page credentials, or a backend for each generated page.

Display-only resources can be used in any frontend. Interactive reply resources
are for pages opened through Ava's authenticated page URL. The browser submits
user input; the receiving agent interprets that input within the task's authority.
