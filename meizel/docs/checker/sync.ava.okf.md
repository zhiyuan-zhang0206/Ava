---
type: doc
title: "meizel/ — Keeping Manifests Current"
description: "sync --prune and sync --declare: how a change brings the meizel.toml files in line with the dependencies it adds and removes, and what is deliberately left as an error."
tags:
  - locality
  - tool
---

# meizel/ — Keeping Manifests Current

## What is it

A manifest is an exact map of how a directory's children depend on each other.
A change that adds, moves or removes a dependency has to update it in the
directory where the two sides meet. `sync` does the mechanical part of that.

## `sync --prune`

Removes declared `[uses]` edges and `[loads]` entries that no fact needs,
repeating until nothing stale remains. Comments outside the edited lists
survive; an emptied manifest is deleted.

## `sync --declare [paths...]`

For the fact edges whose source file is under `paths` (default: the files that
differ from the branch point with `origin/main`, working tree and untracked
files included), it writes what the rule asks for:

- the missing `cf -> ct` edge into `[uses]` of the lowest common directory,
  creating the manifest if there is none;
- the missing `exports` entries along the path to the target.

It then does the work of `--prune`. Existing entries are merged and comments
outside the touched lists stay. A manifest it creates must be staged (`git add`)
before `check` sees it, because `check` reads tracked files only.

It declares a dependency only when declaring is the right answer. It leaves the
error in place, names the fact, and exits 1 when the dependency:

- would close a cycle in the local graph (the first of two mutual edges is
  declared, in file order; the second is left): use a public door or inject the
  collaborator instead;
- uses a private name, or goes through an underscore module: use a public name
  or the package door;
- is a restricted Python form.

It never writes `[debt]`: adding recorded debt stays with `debt record`, and
lowering it with `debt prune`. After a rebase the usual order is `check`, then
`sync --declare` for `undeclared-edge` and `not-exported`, then `debt prune`
for `debt-paid`, and a code change for cycles, private names and restricted
forms.

Parent: [[meizel/docs/checker/checker.ava.okf.md|the checker]].
