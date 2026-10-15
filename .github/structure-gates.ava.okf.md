---
type: doc
title: Structure and documentation gates
description: Deterministic merge-tree checks and conditional code generation in backend-structure.
tags: [ci, lint]
---

# Structure and documentation gates

- **CI owns the merge gate.** `backend-structure` partitions its commit-stage
  hooks into lint and conditional codegen; other jobs own excluded checks,
  except the local installation warning. The selector matches the base-to-HEAD
  diff against the four hooks' `files:` regexes in `.pre-commit-config.yaml`.
  Typed-comment validation also checks the committed change against the fetched
  merge base, using the installed TypeScript parser. Manual dispatch runs codegen;
  the queue rechecks its combined tree. See the
  [runbook CI section](../docs/conventions/runbook.md#ci-continuous-integration).

The comment checker runs against the actual checked-out merge tree and its
fetched base revision, including Trunk's combined changes. Missing comparison
ancestry fails the check. The ordinary staged-file hook also remains available
locally, where pre-commit isolates unstaged edits.

Node and locked frontend dependencies are installed before structural checks,
because the TypeScript compiler parses code-comment references on every run.
Only the four existing generation freshness hooks are conditional; selector
failure still runs them. The parser's focused Node tests run in the same job.

The check is deterministic: no model API, semantic-review credential or review
attestation is involved. Reference and source rules are owned by
[[scripts/content_lint/code_comments/docs/code_comments.ava.okf.md]].
