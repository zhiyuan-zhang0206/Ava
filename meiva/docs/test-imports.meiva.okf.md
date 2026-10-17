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

```meiva-contract
id = "parent-import-identity"
kind = "invariant"
statement = "The SDK parent conftest preserves one package identity: already loaded sdk_surface child modules remain attributes of that package during pytest collection."
code = ["meiva/conftest.py:<module>"]
tests = ["meiva/sdk_surface/tests/test_package_identity.py::test_loaded_submodules_are_attributes_of_their_package"]
```

Parent: [[meiva/docs/sdk-surface/sdk-surface.meiva.okf.md]].
