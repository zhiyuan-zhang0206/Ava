---
type: doc
title: Generated API types
description: OpenAPI owns API descriptions; the TypeScript projection contains types only.
tags:
  - tool
  - ci
---

# Generated API types

Run `bash scripts/codegen/codegen-types.sh` to export the backend schema to
`ui/web/openapi.json` and generate `ui/web/src/lib/contracts/types-generated.ts`.
`check-types-fresh.sh` runs the same generator into a temporary directory and
compares both artifacts. Neither generated artifact is edited by hand.

## Type projection

```ava-contract
id = "generated-types"
kind = "invariant"
statement = "The TypeScript projection preserves openapi-typescript's type declarations and omits generated prose while preserving exact machine-readable deprecation markers. The OpenAPI input retains descriptions, examples, defaults and deprecation metadata."
code = ["scripts/codegen/typescript-types.mjs:generateTypes"]
tests = ["scripts/codegen/tests/typescript-types.test.mjs::preserves the upstream type structure and schema while omitting prose", "scripts/codegen/tests/typescript-types.test.mjs::matches the checked-in projection of the complete API schema"]
```

The installed `openapi-typescript` API owns schema-to-type conversion; its
TypeScript AST owns the output. The generator removes synthesized comments and
retains only compiler-recognized deprecation as `/** @deprecated */`.
No text replacement or second schema converter is involved. Regeneration and
freshness checks share `typescript-types.mjs`, including its failure on invalid
references. Tests compare the parsed type structure with the upstream output.

Generated TypeScript hover descriptions disappear; read their schema owner in
`openapi.json`. Deprecation remains available to TypeScript and the existing
`@typescript-eslint/no-deprecated` rule. Types, literal values, nullability,
requiredness and references remain unchanged. This does not authorize deleting
backend docstrings or Pydantic field descriptions that feed OpenAPI.
