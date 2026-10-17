---
type: doc
title: Plugin manifest admission
description: Closed manifest grammar and engine/commit compatibility gates.
tags: [contracts]
---

# Plugin manifest admission

## Closed manifest grammar

A native plugin requires a valid meiva-plugin.json; bare skills and MCP packages may omit it, but a present manifest is always validated. Unknown fields and unsupported version operators fail. Ranges are conjunctions, bare versions mean equality, and prereleases require an explicit prerelease clause.

The [normative spec](../../../../docs/conventions/extensions/plugin-spec-v2.md) owns the accepted identity, dependency, lifecycle and contribution shapes. Validation reports all discovered problems together. The supported range grammar deliberately excludes OR, wildcards, `~=` and `!=`; the pyproject mirror validates its own supported subset instead of approximating unsupported requirements.

## Host compatibility fails closed

A manifest must declare engines or requires_commit. The engine range and commit ancestry checks both apply when both are present. An unavailable commit or a commit not contained by the host is a gate failure, with separate diagnostics.

`host_contract_errors_dir` is shared by installation, runtime package filtering and status. A malformed manifest or an unresolvable check is an error, never an implicit compatibility pass. Operators can distinguish a missing/fetchable SHA from a host that needs upgrading.
