---
type: doc
title: "meizel/ — Patching First-Party Code"
description: "first-party-patch: a test that patches first-party code shows a dependency that was not injected; what counts, what does not, how it is recorded and what to do instead."
tags:
  - locality
  - tool
---

# meizel/ — Patching First-Party Code

## What is it

`first-party-patch` is reported wherever code patches first-party code. A patch
means the code under test reaches its collaborator through a module or class
attribute instead of receiving it. The fix is structural, not a better patch:
split the code into a public facade and an implementation whose dependencies are
explicit parameters, and let the test pass the fake in directly.

## What counts

A call whose target resolves, through the file's own imports, to a first-party
module or object:

- `mock.patch.object(target, "name")` and `mock.patch.multiple(target, ...)`:
  one site per patched name;
- `mock.patch.dict(target, ...)`;
- `monkeypatch.setattr(target, "name", ...)` and `monkeypatch.delattr(target,
  "name")`.

A target is a module (`mod`), an attribute chain (`mod.Client`, `mod.REGISTRY`)
or a first-party name imported into the file (`Client`). It also applies to
fixtures and production code that patch, not only test functions.

String targets (`mock.patch("a.b.c")`) name first-party code without an import
the checker can follow, so they stay `restricted-python` and are not counted a
second time. The object-form patch facts that make an edge to the patched
module are unchanged.

## What does not count

The boundary of this rule, which may be widened by a later decision:

- patching the standard library or a third-party package (`time.sleep`,
  `requests.get`, `os.environ` through `patch.dict`);
- `monkeypatch.setenv` and `monkeypatch.delenv`, and `monkeypatch.setitem` /
  `delitem` on first-party dicts;
- targets the file cannot resolve to an import: a class defined in the same
  file, an argument, `self.attr`.

## Recording

It is an ordinary recordable debt, owned by the directory of the patching file,
keyed by form and dotted target without a line number:

```toml
[debt.first-party-patch]
"test_x.py#monkeypatch.setattr lib.mod.helper" = 2
```

The directory is then strict-to-be like any other: new patches fail, repaid ones
make the entry `debt-paid`. See [[meizel/docs/checker/debt.ava.okf.md|recorded debt]].

Parent: [[meizel/docs/checker/checker.ava.okf.md|the checker]].
