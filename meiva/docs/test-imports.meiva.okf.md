---
type: doc
title: SDK test package import identity
description: Why the SDK keeps an otherwise empty parent conftest.
tags: [sdk, testing]
---

# SDK test package import identity

Keep `meiva/conftest.py` even though it defines no fixture. With pytest's
`--import-mode=importlib`, its presence imports `meiva` as an ordinary parent
before collecting nested conftests. Without that boundary, pytest can execute
`meiva.sdk_surface` twice and replace its package object while loaded children
remain in `sys.modules`, so those children stop being package attributes.

Parent: [[meiva/docs/sdk-surface.meiva.okf.md]].
