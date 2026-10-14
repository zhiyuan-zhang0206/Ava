---
type: doc
title: "meizel/ — Patching"
description: "Every patch is debt: first-party, standard-library, third-party, environment and unresolved targets each have their own code, recordable and shrink-only; what to do instead."
tags:
  - locality
  - tool
---

# meizel/ — Patching

## What is it

A patch means the code under test reaches its collaborator through a module or
class attribute, or through the environment, instead of receiving it. The fix is
structural, not a better patch: split the code into a public facade and an
implementation whose dependencies are explicit parameters, and let the test pass
the fake in directly. Meizel reports every patch, by what is patched:

| code | the target |
|---|---|
| `first-party-patch` | a first-party module or object |
| `stdlib-patch` | the standard library (`time.sleep`, `json.dumps`) |
| `third-party-patch` | any other package (`requests.get`) |
| `env-patch` | the environment: `monkeypatch.setenv/delenv`, `setitem/delitem` or `patch.dict` on `os.environ` |
| `unresolved-patch` | a target the file's imports do not resolve: an argument, `self.attr`, a class defined in the same file, a call result |

## What counts

`mock.patch(...)` with a string target, `mock.patch.object`, `mock.patch.multiple`
(one site per patched name), `mock.patch.dict`, and `monkeypatch.setattr`,
`delattr`, `setitem` and `delitem` on `os.environ`, `setenv`, `delenv`. A target is
classified through the file's imports; a string target names a dotted path. A
first-party string target stays `restricted-python`, since it hides the dependency
from the checker, and is not counted twice; an unreadable string target is
`restricted-python` too. `setitem` and `delitem` on other dicts are not patches
here. It applies to fixtures and production code that patch, not only to tests.

## Recording

Every patch code is ordinary debt, owned by the directory of the patching file and
keyed by form and target without a line number:

```toml
[debt.stdlib-patch]
"test_x.py#monkeypatch.setattr time.sleep" = 2
```

New patches fail in a strict-to-be directory like any other violation; repaid ones
make the entry `debt-paid`. A new `unresolved-patch` is no way around the rule:
only what is already in the baseline passes, and CI fails when the recorded total
grows. See [[meizel/docs/checker/debt.ava.okf.md|recorded debt]]. When a symbol is
renamed, `debt carry --rename-target OLD=NEW` moves the entries to the new name.

Parent: [[meizel/docs/checker/checker.ava.okf.md|the checker]].
