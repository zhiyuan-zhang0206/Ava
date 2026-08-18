---
type: doc
title: testing skill — Testing and Quality Gates
description: Complete check set (pytest / ruff / pyright, vitest / eslint / tsc) + criteria for deciding whether new tests are needed — read after making changes, before declaring completion.
tags:
- extensions
- agent-instruction
---

# testing skill — Testing and Quality Gates

`ava.skills.ava_code.testing`: the quality gate manual after code changes — choose the complete check set to run based on the change surface (Python: pytest / ruff / pyright; frontend: vitest / eslint / tsc), and criteria for "does this change need new tests". Belongs to the scenario deep-dives of [[ava_builtins/plugins/ava_code/skills/skills.ava.okf.md|ava_code carried skills]], loaded before declaring work complete.
