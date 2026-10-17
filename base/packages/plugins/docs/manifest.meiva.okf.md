---
type: doc
title: Plugin manifest admission
description: Closed manifest grammar and engine/commit compatibility gates.
tags: [contracts]
---

# Plugin manifest admission

## Closed manifest grammar

```meiva-contract
id = "closed-manifest-grammar"
kind = "invariant"
statement = "A native plugin requires a valid meiva-plugin.json; bare skills and MCP packages may omit it, but a present manifest is always validated. Unknown fields and unsupported version operators fail. Ranges are conjunctions, bare versions mean equality, and prereleases require an explicit prerelease clause."
code = [
  "base/packages/plugins/manifest.py:parse_range",
  "base/packages/plugins/manifest.py:range_allows",
  "base/packages/plugins/manifest.py:load_manifest",
  "base/packages/plugins/manifest.py:load_optional_manifest",
]
tests = [
  "base/packages/plugins/tests/test_manifest.py::test_parse_range_rejects_unsupported",
  "base/packages/plugins/tests/test_manifest.py::test_parse_range_bare_version_means_eq",
  "base/packages/plugins/tests/test_manifest.py::test_prerelease_requires_an_explicit_prerelease_range",
  "base/packages/plugins/tests/test_manifest.py::test_load_manifest_missing_file_is_refused",
  "base/packages/plugins/tests/test_manifest.py::test_load_optional_manifest_validates_a_present_manifest",
]
```

The [normative spec](../../../../docs/conventions/extensions/plugin-spec-v2.md) owns the accepted identity, dependency, lifecycle and contribution shapes. Validation reports all discovered problems together. The supported range grammar deliberately excludes OR, wildcards, `~=` and `!=`; the pyproject mirror validates its own supported subset instead of approximating unsupported requirements.

## Host compatibility fails closed

```meiva-contract
id = "host-compatibility-fails-closed"
kind = "invariant"
statement = "A manifest must declare engines or requires_commit. The engine range and commit ancestry checks both apply when both are present. An unavailable commit or a commit not contained by the host is a gate failure, with separate diagnostics."
code = [
  "base/packages/plugins/manifest.py:check_host_engine",
  "base/packages/plugins/manifest.py:check_host_commit",
  "base/packages/plugins/manifest.py:host_contract_errors_dir",
]
tests = [
  "base/packages/plugins/tests/test_manifest.py::test_requires_commit_alone_is_a_valid_manifest",
  "base/packages/plugins/tests/test_manifest.py::test_check_host_engine",
  "base/packages/plugins/tests/test_manifest.py::test_check_host_commit_non_ancestor_blocks",
  "base/packages/plugins/tests/test_manifest.py::test_check_host_commit_unresolvable_blocks",
]
```

`host_contract_errors_dir` is shared by installation, runtime package filtering and status. A malformed manifest or an unresolvable check is an error, never an implicit compatibility pass. Operators can distinguish a missing/fetchable SHA from a host that needs upgrading.
